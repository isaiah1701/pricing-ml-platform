"""Compare production observations with training reference data using Evidently."""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

import numpy as np
import pandas as pd
from evidently import DataDefinition, Dataset, Report
from evidently.presets import DataDriftPreset
from prometheus_client import CollectorRegistry, Gauge, generate_latest, push_to_gateway

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE_PATH = PROJECT_ROOT / "data" / "listing_features.parquet"
DEFAULT_PRODUCTION_PATH = PROJECT_ROOT / "data" / "production_features.parquet"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "monitoring" / "reports"
DEFAULT_METRICS_PATH = PROJECT_ROOT / "monitoring" / "metrics" / "drift.prom"
DEFAULT_NUMERICAL_FEATURES: tuple[str, ...] = (
    "latitude",
    "longitude",
    "minimum_nights",
    "number_of_reviews",
    "reviews_per_month",
    "calculated_host_listings_count",
    "availability_365",
    "has_reviews",
    "days_since_last_review",
    "distance_to_midtown_km",
)
DEFAULT_CATEGORICAL_FEATURES: tuple[str, ...] = (
    "neighbourhood_group",
    "neighbourhood",
    "room_type",
    "minimum_nights_band",
    "availability_band",
    "location_cell",
)


class FeatureDriftResult(TypedDict):
    """Serializable drift result for one monitored column."""

    score: float
    threshold: float
    drifted: bool
    kind: Literal["numerical", "categorical", "prediction"]


class DataQualityResult(TypedDict):
    """Bounded production-window data-quality measurements."""

    input_rows: int
    valid_rows: int
    invalid_rows: int
    missing_ratios: dict[str, float]
    unseen_category_ratios: dict[str, float]
    source_modified_timestamp: str
    source_age_seconds: float


class DriftResult(TypedDict):
    """Serializable summary and artifact locations for one drift run."""

    timestamp: str
    model_version: str
    reference_path: str
    production_path: str
    production_window_rows: int
    dataset_drift: bool
    drifted_feature_count: int
    monitored_feature_count: int
    drift_share: float
    prediction_drift: bool
    features: dict[str, FeatureDriftResult]
    data_quality: DataQualityResult
    json_report_path: str
    html_report_path: str
    metrics_path: str


def _read_dataset(path: Path) -> pd.DataFrame:
    """Load a supported monitoring dataset from Parquet or CSV."""
    if not path.is_file():
        LOGGER.error("Monitoring dataset does not exist: %s", path)
        raise FileNotFoundError(f"Monitoring dataset not found: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    LOGGER.error("Unsupported monitoring dataset format: %s", path.suffix)
    raise ValueError("Monitoring datasets must be Parquet or CSV")


def _extract_scores(
    report_data: dict[str, object],
    column_kinds: dict[str, Literal["numerical", "categorical", "prediction"]],
    threshold: float,
) -> dict[str, FeatureDriftResult]:
    """Extract consistently interpreted PSI scores from an Evidently report."""
    results: dict[str, FeatureDriftResult] = {}
    metrics = report_data.get("metrics", [])
    if not isinstance(metrics, list):
        raise TypeError("Evidently report did not contain a metric list")
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        config = metric.get("config")
        if not isinstance(config, dict) or config.get("type") != "evidently:metric_v2:ValueDrift":
            continue
        column = config.get("column")
        value = metric.get("value")
        if isinstance(column, str) and column in column_kinds and isinstance(
            value, (int, float)
        ):
            score = float(value)
            results[column] = {
                "score": score,
                "threshold": threshold,
                "drifted": score > threshold,
                "kind": column_kinds[column],
            }
    missing = sorted(set(column_kinds).difference(results))
    if missing:
        LOGGER.error("Evidently omitted drift metrics for columns: %s", missing)
        raise ValueError(f"Evidently omitted drift metrics for columns: {missing}")
    return results


def write_prometheus_metrics(result: DriftResult, metrics_path: Path) -> None:
    """Atomically publish drift results for a Prometheus textfile collector."""
    registry = CollectorRegistry()
    feature_score = Gauge(
        "airbnb_feature_drift_score",
        "Per-column Evidently PSI drift score.",
        ("feature", "kind", "model_version"),
        registry=registry,
    )
    for feature, values in result["features"].items():
        feature_score.labels(feature, values["kind"], result["model_version"]).set(
            values["score"]
        )
    Gauge(
        "airbnb_dataset_drift",
        "Whether the configured share of monitored columns drifted.",
        registry=registry,
    ).set(float(result["dataset_drift"]))
    Gauge(
        "airbnb_drifted_features",
        "Number of monitored columns with drift.",
        registry=registry,
    ).set(result["drifted_feature_count"])
    Gauge(
        "airbnb_prediction_drift",
        "Whether the prediction distribution drifted.",
        registry=registry,
    ).set(float(result["prediction_drift"]))
    Gauge(
        "airbnb_drift_last_success_timestamp_seconds",
        "Unix timestamp of the last successful drift check.",
        registry=registry,
    ).set(datetime.fromisoformat(result["timestamp"]).timestamp())
    missing_ratio = Gauge(
        "airbnb_data_missing_ratio",
        "Share of production-window values missing by feature.",
        ("feature",),
        registry=registry,
    )
    for feature, value in result["data_quality"]["missing_ratios"].items():
        missing_ratio.labels(feature).set(value)
    unseen_ratio = Gauge(
        "airbnb_data_unseen_category_ratio",
        "Share of production-window values absent from reference categories.",
        ("feature",),
        registry=registry,
    )
    for feature, value in result["data_quality"]["unseen_category_ratios"].items():
        unseen_ratio.labels(feature).set(value)
    Gauge(
        "airbnb_data_quality_invalid_rows",
        "Production-window rows excluded for missing, non-finite, or unseen values.",
        registry=registry,
    ).set(result["data_quality"]["invalid_rows"])
    Gauge(
        "airbnb_production_window_rows",
        "Rows received in the current production monitoring window.",
        registry=registry,
    ).set(result["data_quality"]["input_rows"])
    Gauge(
        "airbnb_data_quality_valid_ratio",
        "Share of production-window rows valid for drift analysis.",
        registry=registry,
    ).set(
        result["data_quality"]["valid_rows"]
        / result["data_quality"]["input_rows"]
    )
    Gauge(
        "airbnb_production_data_modified_timestamp_seconds",
        "Unix modification timestamp of the production monitoring dataset.",
        registry=registry,
    ).set(
        datetime.fromisoformat(
            result["data_quality"]["source_modified_timestamp"]
        ).timestamp()
    )

    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=metrics_path.parent, delete=False
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        temporary_file.write(generate_latest(registry))
    os.replace(temporary_path, metrics_path)
    LOGGER.info("Published drift metrics to %s", metrics_path)
    pushgateway_url = os.getenv("PROMETHEUS_PUSHGATEWAY_URL", "").strip()
    if pushgateway_url:
        push_to_gateway(pushgateway_url, job="airbnb-drift-monitoring", registry=registry)
        LOGGER.info("Published drift metrics to the configured Pushgateway")


def run_drift_check(
    reference_path: str | Path,
    production_path: str | Path,
    output_dir: str | Path,
    metrics_path: str | Path,
    *,
    numerical_features: tuple[str, ...] = DEFAULT_NUMERICAL_FEATURES,
    categorical_features: tuple[str, ...] = DEFAULT_CATEGORICAL_FEATURES,
    prediction_column: str = "prediction",
    reference_prediction_column: str = "price_per_night",
    production_window_rows: int = 10_000,
    drift_threshold: float = 0.2,
    dataset_drift_share_threshold: float = 0.5,
    model_version: str = "unknown",
) -> DriftResult:
    """Run Evidently drift analysis and persist JSON, HTML, and Prometheus outputs."""
    if production_window_rows < 1:
        raise ValueError("production_window_rows must be positive")
    if not 0.0 < drift_threshold <= 1.0:
        raise ValueError("drift_threshold must be in (0, 1]")
    if not 0.0 <= dataset_drift_share_threshold <= 1.0:
        raise ValueError("dataset_drift_share_threshold must be in [0, 1]")

    resolved_reference = Path(reference_path).expanduser().resolve()
    resolved_production = Path(production_path).expanduser().resolve()
    reference = _read_dataset(resolved_reference)
    production = _read_dataset(resolved_production).tail(production_window_rows).copy()
    monitored = [*numerical_features, *categorical_features, prediction_column]
    missing_reference = sorted(
        set(numerical_features + categorical_features).difference(reference.columns)
    )
    missing_production = sorted(set(monitored).difference(production.columns))
    if missing_reference or missing_production:
        LOGGER.error(
            "Drift inputs are missing columns; reference=%s production=%s",
            missing_reference,
            missing_production,
        )
        raise ValueError(
            f"Missing monitoring columns: reference={missing_reference}, "
            f"production={missing_production}"
        )
    if prediction_column not in reference.columns:
        if reference_prediction_column not in reference.columns:
            raise ValueError(
                "Reference dataset requires either the prediction or reference prediction column"
            )
        reference = reference.rename(
            columns={reference_prediction_column: prediction_column}
        )

    checked_at = datetime.now(timezone.utc)
    missing_ratios = {
        column: float(production[column].isna().mean()) for column in monitored
    }
    unseen_category_ratios: dict[str, float] = {}
    invalid_mask = production.loc[:, monitored].isna().any(axis=1)
    for column in numerical_features + (prediction_column,):
        numeric_values = pd.to_numeric(production[column], errors="coerce")
        invalid_mask |= ~numeric_values.map(np.isfinite)
    quality_invalid_mask = invalid_mask.copy()
    for column in categorical_features:
        reference_values = set(reference[column].dropna().astype(str))
        production_values = production[column].astype("string")
        unseen = production_values.notna() & ~production_values.isin(reference_values)
        unseen_category_ratios[column] = float(unseen.mean())
        quality_invalid_mask |= unseen
    source_modified = datetime.fromtimestamp(
        resolved_production.stat().st_mtime, tz=timezone.utc
    )
    data_quality: DataQualityResult = {
        "input_rows": len(production),
        "valid_rows": int((~quality_invalid_mask).sum()),
        "invalid_rows": int(quality_invalid_mask.sum()),
        "missing_ratios": missing_ratios,
        "unseen_category_ratios": unseen_category_ratios,
        "source_modified_timestamp": source_modified.isoformat(),
        "source_age_seconds": max(
            0.0, (checked_at - source_modified).total_seconds()
        ),
    }
    reference = reference.loc[:, monitored].dropna()
    production = production.loc[~invalid_mask, monitored].copy()
    if reference.empty or production.empty:
        raise ValueError("Reference and production monitoring windows must contain rows")
    column_kinds: dict[
        str, Literal["numerical", "categorical", "prediction"]
    ] = {
        **{column: "numerical" for column in numerical_features},
        **{column: "categorical" for column in categorical_features},
        prediction_column: "prediction",
    }
    definition = DataDefinition(
        numerical_columns=[*numerical_features, prediction_column],
        categorical_columns=list(categorical_features),
    )
    report = Report(
        [
            DataDriftPreset(
                columns=monitored,
                method="psi",
                threshold=drift_threshold,
                drift_share=dataset_drift_share_threshold,
                include_tests=True,
            )
        ],
        metadata={
            "model_version": model_version,
            "production_window_rows": str(len(production)),
        },
        model_id=model_version,
        dataset_id=resolved_production.name,
    )
    snapshot = report.run(
        Dataset.from_pandas(production, data_definition=definition),
        Dataset.from_pandas(reference, data_definition=definition),
        timestamp=checked_at,
    )
    features = _extract_scores(snapshot.dict(), column_kinds, drift_threshold)
    drifted_count = sum(value["drifted"] for value in features.values())
    drift_share = drifted_count / len(features)
    timestamp = checked_at.isoformat()
    report_directory = Path(output_dir).expanduser().resolve()
    report_directory.mkdir(parents=True, exist_ok=True)
    filename_timestamp = checked_at.strftime("%Y%m%dT%H%M%SZ")
    json_path = report_directory / f"drift-{filename_timestamp}.json"
    html_path = report_directory / f"drift-{filename_timestamp}.html"
    resolved_metrics_path = Path(metrics_path).expanduser().resolve()
    result: DriftResult = {
        "timestamp": timestamp,
        "model_version": model_version,
        "reference_path": str(resolved_reference),
        "production_path": str(resolved_production),
        "production_window_rows": len(production),
        "dataset_drift": drift_share >= dataset_drift_share_threshold,
        "drifted_feature_count": drifted_count,
        "monitored_feature_count": len(features),
        "drift_share": drift_share,
        "prediction_drift": features[prediction_column]["drifted"],
        "features": features,
        "data_quality": data_quality,
        "json_report_path": str(json_path),
        "html_report_path": str(html_path),
        "metrics_path": str(resolved_metrics_path),
    }
    json_path.write_text(
        json.dumps(
            {"summary": result, "evidently": snapshot.dict()}, indent=2, default=str
        ),
        encoding="utf-8",
    )
    snapshot.save_html(str(html_path))
    write_prometheus_metrics(result, resolved_metrics_path)
    LOGGER.info(
        "Drift check completed: %d/%d columns drifted",
        drifted_count,
        len(features),
    )
    return result


def _parse_columns(value: str) -> tuple[str, ...]:
    """Parse a comma-separated, nonempty feature list."""
    columns = tuple(column.strip() for column in value.split(",") if column.strip())
    if not columns:
        raise argparse.ArgumentTypeError("at least one feature is required")
    return columns


def _parse_args() -> argparse.Namespace:
    """Parse command-line drift-check configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-path", type=Path, default=DEFAULT_REFERENCE_PATH)
    parser.add_argument("--production-path", type=Path, default=DEFAULT_PRODUCTION_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--metrics-path", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--numerical-features", type=_parse_columns, default=DEFAULT_NUMERICAL_FEATURES)
    parser.add_argument("--categorical-features", type=_parse_columns, default=DEFAULT_CATEGORICAL_FEATURES)
    parser.add_argument("--prediction-column", default="prediction")
    parser.add_argument("--reference-prediction-column", default="price_per_night")
    parser.add_argument("--production-window-rows", type=int, default=10_000)
    parser.add_argument("--drift-threshold", type=float, default=0.2)
    parser.add_argument("--dataset-drift-share-threshold", type=float, default=0.5)
    parser.add_argument("--model-version", default=os.getenv("MODEL_VERSION", "unknown"))
    return parser.parse_args()


def main() -> None:
    """Configure logging and execute the configured drift check."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parse_args()
    result = run_drift_check(
        arguments.reference_path,
        arguments.production_path,
        arguments.output_dir,
        arguments.metrics_path,
        numerical_features=arguments.numerical_features,
        categorical_features=arguments.categorical_features,
        prediction_column=arguments.prediction_column,
        reference_prediction_column=arguments.reference_prediction_column,
        production_window_rows=arguments.production_window_rows,
        drift_threshold=arguments.drift_threshold,
        dataset_drift_share_threshold=arguments.dataset_drift_share_threshold,
        model_version=arguments.model_version,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
