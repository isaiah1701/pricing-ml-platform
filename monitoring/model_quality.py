"""Calculate delayed production model-quality metrics from matched outcomes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import numpy as np
import pandas as pd
from prometheus_client import CollectorRegistry, Gauge, generate_latest, push_to_gateway
from sklearn.metrics import mean_absolute_error, r2_score, root_mean_squared_error

from monitoring.operational_metrics import persist_registry

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTCOMES_PATH = PROJECT_ROOT / "data" / "model_outcomes.parquet"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "monitoring" / "reports"
DEFAULT_METRICS_PATH = PROJECT_ROOT / "monitoring" / "metrics" / "model_quality.prom"
DEFAULT_SEGMENT_COLUMNS: tuple[str, ...] = ("room_type", "neighbourhood_group")
REQUIRED_COLUMNS: tuple[str, ...] = (
    "prediction_id",
    "predicted_price",
    "actual_price",
    "model_version",
    "outcome_timestamp",
)


class QualitySlice(TypedDict):
    """Serializable quality measurements for one model and bounded segment."""

    model_version: str
    segment: str
    segment_value: str
    samples: int
    mae_dollars: float
    rmse_dollars: float
    median_absolute_error_dollars: float
    mean_bias_dollars: float
    within_25_dollars_ratio: float
    within_20_percent_ratio: float
    r2: float | None


class ModelQualityResult(TypedDict):
    """Serializable summary and artifact locations for one quality run."""

    timestamp: str
    outcomes_path: str
    window_days: int
    input_rows: int
    matched_outcomes: int
    invalid_rows: int
    duplicate_prediction_ids: int
    match_ratio: float
    latest_outcome_timestamp: str
    outcome_age_seconds: float
    reference_mae_dollars: float
    slices: list[QualitySlice]
    report_path: str
    metrics_path: str


def _read_dataset(path: Path) -> pd.DataFrame:
    """Load a supported matched-outcome dataset from Parquet or CSV."""
    if not path.is_file():
        LOGGER.error("Matched-outcome dataset does not exist: %s", path)
        raise FileNotFoundError(f"Matched-outcome dataset not found: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    LOGGER.error("Unsupported matched-outcome format: %s", path.suffix)
    raise ValueError("Matched outcomes must be Parquet or CSV")


def _calculate_slice(
    frame: pd.DataFrame,
    model_version: str,
    segment: str,
    segment_value: str,
) -> QualitySlice:
    """Calculate regression quality for one model-version segment."""
    actual = frame["actual_price"].to_numpy(dtype=float)
    predicted = frame["predicted_price"].to_numpy(dtype=float)
    errors = predicted - actual
    percentage_errors = np.abs(errors) / actual
    r2: float | None = None
    if len(frame) >= 2 and not np.all(actual == actual[0]):
        r2 = float(r2_score(actual, predicted))
    return {
        "model_version": model_version,
        "segment": segment,
        "segment_value": segment_value,
        "samples": len(frame),
        "mae_dollars": float(mean_absolute_error(actual, predicted)),
        "rmse_dollars": float(root_mean_squared_error(actual, predicted)),
        "median_absolute_error_dollars": float(np.median(np.abs(errors))),
        "mean_bias_dollars": float(np.mean(errors)),
        "within_25_dollars_ratio": float(np.mean(np.abs(errors) <= 25.0)),
        "within_20_percent_ratio": float(np.mean(percentage_errors <= 0.2)),
        "r2": r2,
    }


def _calculate_slices(
    outcomes: pd.DataFrame, segment_columns: tuple[str, ...]
) -> list[QualitySlice]:
    """Calculate overall and bounded segment metrics for each model version."""
    slices: list[QualitySlice] = []
    prepared = outcomes.copy()
    prepared["actual_price_band"] = pd.cut(
        prepared["actual_price"],
        bins=[0.0, 250.0, 500.0, float("inf")],
        labels=["at_or_below_250", "above_250_to_500", "above_500"],
        include_lowest=True,
    ).astype(str)
    available_segments = tuple(
        column
        for column in (*segment_columns, "actual_price_band")
        if column in prepared.columns
    )
    for version, version_frame in prepared.groupby("model_version", sort=True):
        model_version = str(version)
        slices.append(
            _calculate_slice(version_frame, model_version, "overall", "all")
        )
        for segment in available_segments:
            for value, segment_frame in version_frame.groupby(segment, sort=True):
                slices.append(
                    _calculate_slice(
                        segment_frame,
                        model_version,
                        segment,
                        str(value),
                    )
                )
    return slices


def write_prometheus_metrics(
    result: ModelQualityResult, metrics_path: Path
) -> None:
    """Atomically publish production quality metrics for a textfile collector."""
    registry = CollectorRegistry()
    labels = ("model_version", "segment", "segment_value")
    metric_definitions = {
        "mae_dollars": Gauge(
            "airbnb_model_quality_mae_dollars",
            "Mean absolute production error in dollars.",
            labels,
            registry=registry,
        ),
        "rmse_dollars": Gauge(
            "airbnb_model_quality_rmse_dollars",
            "Production root mean squared error in dollars.",
            labels,
            registry=registry,
        ),
        "median_absolute_error_dollars": Gauge(
            "airbnb_model_quality_median_absolute_error_dollars",
            "Median absolute production error in dollars.",
            labels,
            registry=registry,
        ),
        "mean_bias_dollars": Gauge(
            "airbnb_model_quality_mean_bias_dollars",
            "Signed mean production error in dollars.",
            labels,
            registry=registry,
        ),
        "within_25_dollars_ratio": Gauge(
            "airbnb_model_quality_within_25_dollars_ratio",
            "Share of predictions within 25 dollars of the actual outcome.",
            labels,
            registry=registry,
        ),
        "within_20_percent_ratio": Gauge(
            "airbnb_model_quality_within_20_percent_ratio",
            "Share of predictions within 20 percent of the actual outcome.",
            labels,
            registry=registry,
        ),
        "samples": Gauge(
            "airbnb_model_quality_samples",
            "Matched outcomes included in a production-quality slice.",
            labels,
            registry=registry,
        ),
        "r2": Gauge(
            "airbnb_model_quality_r2",
            "Production coefficient of determination.",
            labels,
            registry=registry,
        ),
    }
    degradation = Gauge(
        "airbnb_model_quality_mae_degradation_ratio",
        "Overall production MAE divided by the validated reference MAE.",
        ("model_version",),
        registry=registry,
    )
    for quality_slice in result["slices"]:
        label_values = (
            quality_slice["model_version"],
            quality_slice["segment"],
            quality_slice["segment_value"],
        )
        for key, gauge in metric_definitions.items():
            value = quality_slice[key]  # type: ignore[literal-required]
            if value is not None:
                gauge.labels(*label_values).set(float(value))
        if quality_slice["segment"] == "overall":
            degradation.labels(quality_slice["model_version"]).set(
                quality_slice["mae_dollars"] / result["reference_mae_dollars"]
            )
    Gauge(
        "airbnb_model_quality_match_ratio",
        "Share of input outcome rows validly matched to predictions.",
        registry=registry,
    ).set(result["match_ratio"])
    Gauge(
        "airbnb_model_quality_input_rows",
        "Outcome rows received within the rolling quality window.",
        registry=registry,
    ).set(result["input_rows"])
    Gauge(
        "airbnb_model_quality_invalid_rows",
        "Invalid rows excluded from production-quality calculations.",
        registry=registry,
    ).set(result["invalid_rows"])
    Gauge(
        "airbnb_model_quality_duplicate_prediction_ids",
        "Duplicate prediction identifiers excluded from quality calculations.",
        registry=registry,
    ).set(result["duplicate_prediction_ids"])
    Gauge(
        "airbnb_model_quality_latest_outcome_timestamp_seconds",
        "Unix timestamp of the newest matched production outcome.",
        registry=registry,
    ).set(datetime.fromisoformat(result["latest_outcome_timestamp"]).timestamp())
    Gauge(
        "airbnb_model_quality_last_success_timestamp_seconds",
        "Unix timestamp of the last successful model-quality calculation.",
        registry=registry,
    ).set(datetime.fromisoformat(result["timestamp"]).timestamp())

    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=metrics_path.parent, delete=False
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        temporary_file.write(generate_latest(registry))
    os.replace(temporary_path, metrics_path)
    LOGGER.info("Published model-quality metrics to %s", metrics_path)
    if os.getenv("PLATFORM_DATA_BUCKET", "").strip():
        persist_registry("model-quality", registry, datetime.fromisoformat(result["timestamp"]).timestamp())
        return
    pushgateway_url = os.getenv("PROMETHEUS_PUSHGATEWAY_URL", "").strip()
    if pushgateway_url:
        push_to_gateway(
            pushgateway_url,
            job="airbnb-model-quality-monitoring",
            registry=registry,
        )
        LOGGER.info("Published model-quality metrics to the configured Pushgateway")


def run_model_quality_check(
    outcomes_path: str | Path,
    output_dir: str | Path,
    metrics_path: str | Path,
    *,
    window_days: int = 30,
    reference_mae_dollars: float = 54.67,
    segment_columns: tuple[str, ...] = DEFAULT_SEGMENT_COLUMNS,
    checked_at: datetime | None = None,
) -> ModelQualityResult:
    """Calculate rolling quality from valid outcomes and persist report artifacts."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    if reference_mae_dollars <= 0.0:
        raise ValueError("reference_mae_dollars must be positive")
    resolved_outcomes = Path(outcomes_path).expanduser().resolve()
    frame = _read_dataset(resolved_outcomes)
    missing = sorted(set(REQUIRED_COLUMNS).difference(frame.columns))
    if missing:
        LOGGER.error("Matched outcomes are missing columns: %s", missing)
        raise ValueError(f"Matched outcomes are missing columns: {missing}")
    timestamp = checked_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    all_timestamps = pd.to_datetime(
        frame["outcome_timestamp"], utc=True, errors="coerce"
    )
    window_start = pd.Timestamp(timestamp - timedelta(days=window_days))
    scope_mask = all_timestamps.isna() | all_timestamps.ge(window_start)
    scoped = frame.loc[scope_mask].copy()
    timestamps = all_timestamps.loc[scoped.index]
    predicted = pd.to_numeric(scoped["predicted_price"], errors="coerce")
    actual = pd.to_numeric(scoped["actual_price"], errors="coerce")
    duplicate_mask = scoped["prediction_id"].duplicated(keep=False)
    duplicate_count = int(duplicate_mask.sum())
    invalid_mask = (
        scoped[list(REQUIRED_COLUMNS)].isna().any(axis=1)
        | timestamps.isna()
        | timestamps.gt(pd.Timestamp(timestamp))
        | ~np.isfinite(predicted)
        | ~np.isfinite(actual)
        | (predicted < 0.0)
        | (actual <= 0.0)
        | duplicate_mask
    )
    valid = scoped.loc[~invalid_mask].copy()
    valid["predicted_price"] = predicted.loc[valid.index]
    valid["actual_price"] = actual.loc[valid.index]
    valid["outcome_timestamp"] = timestamps.loc[valid.index]
    valid["model_version"] = valid["model_version"].astype(str)
    if valid.empty:
        LOGGER.error("No valid matched outcomes were available in the rolling window")
        raise ValueError("No valid matched outcomes available in the rolling window")
    invalid_rows = int(invalid_mask.sum())
    slices = _calculate_slices(valid, segment_columns)
    latest_outcome = valid["outcome_timestamp"].max().to_pydatetime()
    report_directory = Path(output_dir).expanduser().resolve()
    report_directory.mkdir(parents=True, exist_ok=True)
    report_path = report_directory / (
        f"model-quality-{timestamp.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    resolved_metrics = Path(metrics_path).expanduser().resolve()
    result: ModelQualityResult = {
        "timestamp": timestamp.isoformat(),
        "outcomes_path": str(resolved_outcomes),
        "window_days": window_days,
        "input_rows": len(scoped),
        "matched_outcomes": len(valid),
        "invalid_rows": invalid_rows,
        "duplicate_prediction_ids": duplicate_count,
        "match_ratio": len(valid) / len(scoped) if len(scoped) else 0.0,
        "latest_outcome_timestamp": latest_outcome.isoformat(),
        "outcome_age_seconds": max(0.0, (timestamp - latest_outcome).total_seconds()),
        "reference_mae_dollars": reference_mae_dollars,
        "slices": slices,
        "report_path": str(report_path),
        "metrics_path": str(resolved_metrics),
    }
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_prometheus_metrics(result, resolved_metrics)
    LOGGER.info(
        "Model-quality check completed with %d valid outcomes across %d slices",
        len(valid),
        len(slices),
    )
    return result


def _parse_columns(value: str) -> tuple[str, ...]:
    """Parse a comma-separated segment-column list."""
    return tuple(column.strip() for column in value.split(",") if column.strip())


def _parse_args() -> argparse.Namespace:
    """Parse model-quality command-line configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes-path", type=Path, default=DEFAULT_OUTCOMES_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--metrics-path", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--reference-mae-dollars", type=float, default=54.67)
    parser.add_argument(
        "--segment-columns", type=_parse_columns, default=DEFAULT_SEGMENT_COLUMNS
    )
    return parser.parse_args()


def main() -> None:
    """Configure logging and run the production model-quality calculation."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parse_args()
    result = run_model_quality_check(
        arguments.outcomes_path,
        arguments.output_dir,
        arguments.metrics_path,
        window_days=arguments.window_days,
        reference_mae_dollars=arguments.reference_mae_dollars,
        segment_columns=arguments.segment_columns,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
