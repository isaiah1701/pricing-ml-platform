"""Train and log the champion Airbnb nightly-price model."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from mlflow import MlflowClient
from mlflow.models import infer_signature
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.dummy import DummyRegressor
from sklearn.metrics import mean_absolute_error, r2_score, root_mean_squared_error
from sklearn.model_selection import train_test_split
from sklearn.utils.validation import check_is_fitted

if TYPE_CHECKING:
    from feast.feature_service import FeatureService

    from training.provenance import CodeProvenance

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO_PATH = PROJECT_ROOT / "feature_repo"
DEFAULT_DATA_PATH = PROJECT_ROOT / "data" / "listing_features.parquet"
DEFAULT_RAW_DATA_PATH = PROJECT_ROOT / "data" / "AB_NYC_2019.csv"
DEFAULT_SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"
FEATURE_SERVICE_NAME = "airbnb_price_model_v1"
EXPERIMENT_NAME = "airbnb-price-prediction"
MODEL_NAME = "catboost_log_price_seed_ensemble"
TARGET_COLUMN = "price_per_night"
ENTITY_COLUMNS: tuple[str, str] = ("id", "event_timestamp")
CATEGORICAL_FEATURES: tuple[str, ...] = (
    "neighbourhood_group",
    "neighbourhood",
    "room_type",
    "minimum_nights_band",
    "availability_band",
    "location_cell",
    "neighbourhood_room_type",
)
EXCLUDED_FEATURES: frozenset[str] = frozenset(
    {"availability_ratio", "neighbourhood_room_type"}
)
RANDOM_STATE = 42
TEST_SIZE = 0.2
ENSEMBLE_RANDOM_STATES: tuple[int, ...] = (42, 7, 123)
ENSEMBLE_ITERATIONS: tuple[int, ...] = (1440, 1565, 1589)
LEARNING_RATE = 0.04
TREE_DEPTH = 10
L2_LEAF_REG = 8.0
RANDOM_STRENGTH = 1.0
CODE_SNAPSHOT_DIRECTORY = Path(
    os.getenv("AIRBNB_CODE_SNAPSHOT_DIR", "/tmp/airbnb-code-snapshots")
)


def load_labels(data_path: Path) -> pd.DataFrame:
    """Load validated entity metadata and nightly-price labels."""
    import pyarrow.parquet as pq

    LOGGER.info("Loading training labels from %s", data_path)
    if not data_path.is_file():
        LOGGER.error("Transformed dataset does not exist: %s", data_path)
        raise FileNotFoundError(f"Transformed Parquet dataset not found: {data_path}")
    required = (*ENTITY_COLUMNS, TARGET_COLUMN)
    missing = sorted(set(required).difference(pq.read_schema(data_path).names))
    if missing:
        LOGGER.error("Training labels are missing columns: %s", missing)
        raise ValueError(f"Transformed dataset is missing columns: {missing}")
    labels = pd.read_parquet(data_path, columns=list(required))
    if labels.empty or labels["id"].isna().any():
        LOGGER.error("Training labels are empty or contain null listing IDs")
        raise ValueError("Training labels require rows and non-null IDs")
    labels["event_timestamp"] = pd.to_datetime(
        labels["event_timestamp"], errors="coerce", utc=True
    )
    if labels["event_timestamp"].isna().any():
        LOGGER.error("Training labels contain invalid timestamps")
        raise ValueError("event_timestamp must contain valid values")
    labels[TARGET_COLUMN] = pd.to_numeric(labels[TARGET_COLUMN], errors="coerce")
    target = labels[TARGET_COLUMN].to_numpy()
    if not np.isfinite(target).all() or (target <= 0).any():
        LOGGER.error("Training labels contain invalid nightly prices")
        raise ValueError(f"{TARGET_COLUMN} must contain finite positive values")
    if labels.duplicated(subset=list(ENTITY_COLUMNS)).any():
        LOGGER.error("Training labels contain duplicate entity timestamp keys")
        raise ValueError("id and event_timestamp must uniquely identify rows")
    LOGGER.info("Loaded %d training label rows", len(labels))
    return labels


def get_feature_names(feature_service: FeatureService) -> tuple[str, ...]:
    """Return validated predictive feature names from a Feast service."""
    names = tuple(
        feature.name
        for projection in feature_service.feature_view_projections
        for feature in projection.features
    )
    if not names or len(names) != len(set(names)):
        LOGGER.error("FeatureService must contain unique predictive features")
        raise ValueError("FeatureService must contain unique predictive features")
    leaked = sorted({*ENTITY_COLUMNS, TARGET_COLUMN}.intersection(names))
    if leaked:
        LOGGER.error("FeatureService contains forbidden inputs: %s", leaked)
        raise ValueError(f"FeatureService contains forbidden model inputs: {leaked}")
    return names


def load_features_from_feast(
    feature_repo_path: Path,
    entity_df: pd.DataFrame,
    feature_service_name: str = FEATURE_SERVICE_NAME,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Retrieve point-in-time-correct model features from Feast."""
    from feast import FeatureStore
    from feast.errors import FeatureServiceNotFoundException

    LOGGER.info("Loading Feast feature store from %s", feature_repo_path)
    store = FeatureStore(repo_path=str(feature_repo_path))
    try:
        service = store.get_feature_service(feature_service_name)
    except FeatureServiceNotFoundException as exc:
        LOGGER.error("FeatureService %s was not found", feature_service_name)
        raise ValueError(
            f"Feast FeatureService {feature_service_name!r} was not found"
        ) from exc
    names = get_feature_names(service)
    LOGGER.info("Retrieving %d historical features", len(names))
    historical = store.get_historical_features(
        entity_df=entity_df.loc[:, list(ENTITY_COLUMNS)], features=service
    ).to_df()
    return historical, names


def build_training_dataset(
    labels: pd.DataFrame,
    historical_features: pd.DataFrame,
    feature_names: Sequence[str],
) -> tuple[pd.DataFrame, pd.Series]:
    """Align Feast features to labels without target or entity leakage."""
    if labels.empty or historical_features.empty:
        LOGGER.error("Cannot build a training dataset with zero rows")
        raise ValueError("Training labels and historical features must contain rows")
    if len(labels) != len(historical_features):
        LOGGER.error("Feature and target row counts do not match")
        raise ValueError("Historical feature and target row counts do not match")
    names = list(feature_names)
    if len(names) != len(set(names)):
        LOGGER.error("Duplicate predictive feature names were supplied")
        raise ValueError("Predictive feature names must be unique")
    missing_labels = sorted({*ENTITY_COLUMNS, TARGET_COLUMN}.difference(labels.columns))
    if missing_labels:
        LOGGER.error("Labels are missing columns: %s", missing_labels)
        raise ValueError(f"Labels are missing required columns: {missing_labels}")
    missing_features = sorted(
        {*ENTITY_COLUMNS, *names}.difference(historical_features.columns)
    )
    if missing_features:
        LOGGER.error("Historical features are missing: %s", missing_features)
        raise ValueError(f"Historical features are missing columns: {missing_features}")
    if TARGET_COLUMN in historical_features.columns or TARGET_COLUMN in names:
        LOGGER.error("Target leakage detected in historical features")
        raise ValueError(f"Unexpected {TARGET_COLUMN} inside model features")
    if set(ENTITY_COLUMNS).intersection(names):
        LOGGER.error("Entity metadata was selected as predictive input")
        raise ValueError("Entity metadata cannot be model features")

    aligned_labels = labels.copy()
    aligned_features = historical_features.copy()
    for frame in (aligned_labels, aligned_features):
        frame["event_timestamp"] = pd.to_datetime(
            frame["event_timestamp"], errors="coerce", utc=True
        )
    if aligned_features[list(ENTITY_COLUMNS)].isna().any().any():
        LOGGER.error("Historical features contain null entity metadata")
        raise ValueError("Historical features contain null entity metadata")
    if aligned_features.duplicated(subset=list(ENTITY_COLUMNS)).any():
        LOGGER.error("Historical features contain duplicate entity keys")
        raise ValueError("Historical features contain duplicate entity timestamp keys")
    label_index = pd.MultiIndex.from_frame(aligned_labels.loc[:, list(ENTITY_COLUMNS)])
    feature_index = pd.MultiIndex.from_frame(
        aligned_features.loc[:, list(ENTITY_COLUMNS)]
    )
    if (
        not label_index.difference(feature_index).empty
        or not feature_index.difference(label_index).empty
    ):
        LOGGER.error("Historical feature keys do not align with labels")
        raise ValueError(
            "Historical feature IDs and timestamps do not align with labels"
        )
    x = (
        aligned_features.set_index(list(ENTITY_COLUMNS))
        .reindex(label_index)
        .loc[:, names]
        .reset_index(drop=True)
    )
    y = aligned_labels[TARGET_COLUMN].reset_index(drop=True).rename(TARGET_COLUMN)
    if x.isna().any().any():
        null_columns = x.columns[x.isna().any()].tolist()
        LOGGER.error("Model features contain null values in %s", null_columns)
        raise ValueError(f"Model features contain null values in: {null_columns}")
    LOGGER.info("Built aligned training dataset with %d rows", len(x))
    return x, y


class CatBoostPriceEnsembleRegressor(RegressorMixin, BaseEstimator):
    """Average robust log-price estimates from three CatBoost models."""

    def __init__(
        self,
        feature_names: tuple[str, ...],
        random_states: tuple[int, ...] = ENSEMBLE_RANDOM_STATES,
        iterations: tuple[int, ...] = ENSEMBLE_ITERATIONS,
        learning_rate: float = LEARNING_RATE,
        depth: int = TREE_DEPTH,
        l2_leaf_reg: float = L2_LEAF_REG,
        random_strength: float = RANDOM_STRENGTH,
        checkpoint_dir: str | None = None,
        snapshot_interval: int = 60,
    ) -> None:
        """Store the validation-selected champion configuration."""
        self.feature_names = feature_names
        self.random_states = random_states
        self.iterations = iterations
        self.learning_rate = learning_rate
        self.depth = depth
        self.l2_leaf_reg = l2_leaf_reg
        self.random_strength = random_strength
        self.checkpoint_dir = checkpoint_dir
        self.snapshot_interval = snapshot_interval

    def _prepare_features(self, x: pd.DataFrame) -> pd.DataFrame:
        """Select model inputs and normalize categorical values."""
        if not isinstance(x, pd.DataFrame):
            LOGGER.error("Champion model requires pandas inputs")
            raise TypeError("x must be a pandas DataFrame")
        selected = [
            name for name in self.feature_names if name not in EXCLUDED_FEATURES
        ]
        missing = sorted(set(selected).difference(x.columns))
        if missing:
            LOGGER.error("Champion model inputs are missing: %s", missing)
            raise ValueError(f"x is missing model features: {missing}")
        prepared = x.loc[:, selected].copy()
        for name in CATEGORICAL_FEATURES:
            if name in prepared.columns:
                prepared[name] = prepared[name].astype("string").fillna("unknown")
        return prepared

    def fit(
        self, x: pd.DataFrame, y: pd.Series | np.ndarray
    ) -> CatBoostPriceEnsembleRegressor:
        """Fit or resume identity-bound ensemble members on positive log prices."""
        if not self.random_states or len(self.random_states) != len(self.iterations):
            LOGGER.error("Ensemble seeds and iteration counts do not align")
            raise ValueError(
                "random_states and iterations must be nonempty and have equal length"
            )
        if any(iteration < 1 for iteration in self.iterations):
            LOGGER.error("Ensemble iteration counts must be positive")
            raise ValueError("iterations must contain positive values")
        prepared = self._prepare_features(x)
        target = np.asarray(y, dtype="float64")
        if target.ndim != 1 or len(target) != len(prepared):
            LOGGER.error("Champion feature and target rows do not align")
            raise ValueError("x and y must contain the same number of rows")
        if not np.isfinite(target).all() or (target <= 0).any():
            LOGGER.error("Champion targets must be finite positive prices")
            raise ValueError("y must contain finite positive prices")
        categorical = [
            name for name in CATEGORICAL_FEATURES if name in prepared.columns
        ]
        from contextlib import nullcontext

        # Imported only during training: the serving image needs no checkpoint module.
        from training.checkpoints import (
            checkpoint_directory,
            fit_member,
            training_identity,
        )

        checkpoint_root = self.checkpoint_dir
        if checkpoint_root is None:
            checkpoint_root = os.getenv(
                "AIRBNB_TRAINING_CHECKPOINT_DIR", str(PROJECT_ROOT / "data/training-checkpoints")
            )
        if type(self.snapshot_interval) is not int or self.snapshot_interval < 1:
            raise ValueError("snapshot_interval must be a positive integer")
        common = {
            "loss_function": "RMSE", "learning_rate": self.learning_rate,
            "depth": self.depth, "l2_leaf_reg": self.l2_leaf_reg,
            "random_strength": self.random_strength,
        }
        transformed_target = np.log1p(target)
        identity = training_identity(prepared, transformed_target, {
            **common, "random_states": self.random_states, "iterations": self.iterations,
        })
        session = (checkpoint_directory(Path(checkpoint_root), identity)
                   if checkpoint_root else nullcontext(None))
        self.models_: list[CatBoostRegressor] = []
        with session as directory:
            for member, (random_state, iterations) in enumerate(zip(
                self.random_states, self.iterations, strict=True
            )):
                parameters = {**common, "iterations": iterations, "random_seed": random_state}
                if directory is not None:
                    model = fit_member(prepared, transformed_target, categorical, parameters,
                                       directory, member, self.snapshot_interval)
                else:
                    LOGGER.info("Fitting ensemble member %d without checkpointing", member)
                    model = CatBoostRegressor(**parameters, allow_writing_files=False, verbose=False)
                    model.fit(prepared, transformed_target, cat_features=categorical, verbose=False)
                self.models_.append(model)
        self.feature_names_in_ = np.asarray(prepared.columns, dtype=object)
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        """Return the mean member prediction on the dollar scale."""
        check_is_fitted(self, ("models_", "feature_names_in_"))
        prepared = self._prepare_features(x)
        return np.mean(
            [np.expm1(model.predict(prepared)) for model in self.models_], axis=0
        )

    def feature_importances(self) -> dict[str, float]:
        """Return mean feature importance across champion members."""
        check_is_fitted(self, ("models_", "feature_names_in_"))
        values = np.mean([model.feature_importances_ for model in self.models_], axis=0)
        ordered = sorted(
            zip(self.feature_names_in_, values, strict=True),
            key=lambda item: item[1],
            reverse=True,
        )
        return {str(name): float(value) for name, value in ordered}


def evaluate_model(y_true: pd.Series, y_pred: np.ndarray) -> dict[str, float]:
    """Calculate headline regression metrics in dollars."""
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(root_mean_squared_error(y_true, y_pred)),
        "r2": float(r2_score(y_true, y_pred)),
    }


def evaluate_segments(
    x: pd.DataFrame, y: pd.Series, predictions: np.ndarray
) -> dict[str, float]:
    """Calculate headline and diagnostic segment metrics."""
    metrics = evaluate_model(y, predictions)
    segments = {
        "entire_home": x["room_type"].to_numpy() == "Entire home/apt",
        "private_room": x["room_type"].to_numpy() == "Private room",
        "shared_room": x["room_type"].to_numpy() == "Shared room",
        "at_or_below_250": y.to_numpy() <= 250.0,
        "above_250": y.to_numpy() > 250.0,
        "above_500": y.to_numpy() > 500.0,
        "ordinary_market": y.to_numpy() <= 500.0,
    }
    for name, mask in segments.items():
        positions = np.flatnonzero(mask)
        metrics[f"rows_{name}"] = float(len(positions))
        if len(positions) >= 2:
            scores = evaluate_model(y.iloc[positions], predictions[positions])
            metrics.update(
                {f"{metric}_{name}": value for metric, value in scores.items()}
            )
    metrics["ordinary_market_coverage"] = float(segments["ordinary_market"].mean())
    return metrics


def get_dataset_version(data_path: Path) -> str:
    """Return a deterministic SHA-256 digest of the transformed dataset."""
    from training.lineage import sha256_file

    return sha256_file(data_path)


def get_git_sha(project_root: Path = PROJECT_ROOT) -> str:
    """Return the checked-out Git commit SHA, or ``unknown`` when unavailable."""
    from training.lineage import resolve_git_commit

    return resolve_git_commit(project_root)


def create_training_attempt(
    tracking_uri: str,
    experiment_name: str,
    pipeline_name: str,
) -> str:
    """Create a parent MLflow run before any fallible training work begins."""
    mlflow.set_tracking_uri(tracking_uri)
    experiment = mlflow.set_experiment(experiment_name)
    client = MlflowClient(tracking_uri=tracking_uri)
    created = client.create_run(
        experiment.experiment_id,
        tags={
            "mlflow.runName": f"{pipeline_name}_attempt",
            "run_type": "training_attempt",
            "pipeline_name": pipeline_name,
            "attempt_stage": "started",
            "attempt_outcome": "running",
        },
    )
    LOGGER.info("Created training-attempt run %s", created.info.run_id)
    return str(created.info.run_id)


def update_training_attempt(
    tracking_uri: str,
    run_id: str,
    stage: str,
    *,
    outcome: str = "running",
    error: BaseException | None = None,
) -> None:
    """Update and, when final, terminate a parent training-attempt run."""
    client = MlflowClient(tracking_uri=tracking_uri)
    client.set_tag(run_id, "attempt_stage", stage)
    client.set_tag(run_id, "attempt_outcome", outcome)
    if error is not None:
        client.set_tag(run_id, "failure_type", type(error).__name__)
        client.set_terminated(run_id, status="FAILED")
        LOGGER.error("Training attempt %s failed during %s", run_id, stage)
    elif outcome in {"registered", "rejected", "completed"}:
        client.set_terminated(run_id, status="FINISHED")
        LOGGER.info("Training attempt %s finished with outcome %s", run_id, outcome)


def record_training_attempt_summary(
    tracking_uri: str,
    run_id: str,
    *,
    candidate_count: int,
    winning_model_name: str,
    winning_metrics: dict[str, float],
    dataset_sha256: str,
    registered_model_name: str,
    registered_model_version: str,
    winning_child_run_id: str,
) -> None:
    """Persist the completed training summary on the parent MLflow run."""
    if candidate_count < 1:
        LOGGER.error("Training attempt summary has no evaluated candidates")
        raise ValueError("candidate_count must be positive")
    required_metrics = {"mae", "rmse", "r2"}
    if not required_metrics.issubset(winning_metrics):
        LOGGER.error("Training attempt summary is missing winning metrics")
        raise ValueError("winning_metrics must include mae, rmse, and r2")
    client = MlflowClient(tracking_uri=tracking_uri)
    for key, value in {
        "winning_mae": float(winning_metrics["mae"]),
        "winning_rmse": float(winning_metrics["rmse"]),
        "winning_r2": float(winning_metrics["r2"]),
    }.items():
        client.log_metric(run_id, key, value)
    for key, value in {
        "candidate_count": candidate_count,
        "winning_model_name": winning_model_name,
        "dataset_sha256": dataset_sha256,
        "registered_model_name": registered_model_name,
        "registered_model_version": registered_model_version,
        "winning_child_run_id": winning_child_run_id,
    }.items():
        client.log_param(run_id, key, value)
    client.set_tags(
        run_id,
        {
            "winning_model_name": winning_model_name,
            "dataset_sha256": dataset_sha256,
            "registered_model_name": registered_model_name,
            "registered_model_version": registered_model_version,
            "winning_child_run_id": winning_child_run_id,
        },
    )
    LOGGER.info(
        "Recorded training attempt %s summary for child run %s",
        run_id,
        winning_child_run_id,
    )


def _log_lineage_manifest(
    lineage_manifest_path: str | Path | None,
    dataset_version: str,
) -> str | None:
    """Verify and attach an immutable dataset plus lineage to the active run."""
    from training.lineage import load_lineage_manifest, verify_dataset_snapshot

    if lineage_manifest_path is None:
        LOGGER.warning("Training run has no dataset lineage manifest")
        return None
    resolved_path = Path(lineage_manifest_path).expanduser().resolve()
    manifest = load_lineage_manifest(resolved_path)
    dataset = manifest.get("dataset")
    if not isinstance(dataset, dict) or dataset.get("sha256") != dataset_version:
        LOGGER.error("Lineage manifest does not identify the training dataset")
        raise ValueError("Lineage manifest dataset hash does not match training data")
    lineage_id = str(manifest["lineage_id"])
    mlflow.set_tags(
        {
            "dataset_lineage_id": lineage_id,
            "dataset_snapshot_id": dataset_version,
        }
    )
    mlflow.log_artifact(str(resolved_path), artifact_path="lineage")
    snapshot_path_value = dataset.get("path")
    if not isinstance(snapshot_path_value, str):
        raise TypeError("Lineage manifest does not contain a dataset snapshot path")
    snapshot_path = Path(snapshot_path_value).expanduser().resolve()
    verify_dataset_snapshot(snapshot_path, dataset_version)
    mlflow.log_artifact(str(snapshot_path), artifact_path="data")
    return lineage_id


def _log_code_provenance() -> CodeProvenance:
    """Capture and upload the exact source and dependency files for a run."""
    from training.provenance import build_code_snapshot

    provenance = build_code_snapshot(PROJECT_ROOT, CODE_SNAPSHOT_DIRECTORY)
    mlflow.set_tags(
        {
            "git_sha": provenance["git_commit"],
            "mlflow.source.git.commit": provenance["git_commit"],
            "git_dirty": str(provenance["git_dirty"]).lower(),
            "git_state_available": str(provenance["git_state_available"]).lower(),
            "code_sha256": provenance["code_sha256"],
        }
    )
    mlflow.log_params(
        {
            "code_sha256": provenance["code_sha256"],
            "code_file_count": provenance["file_count"],
        }
    )
    mlflow.log_artifact(provenance["archive_path"], artifact_path="code")
    return provenance


def _log_release_bundle(
    *,
    run_id: str,
    model_uri: str,
    model_artifact_sha256: str,
    dataset_sha256: str,
    lineage_id: str | None,
    code_provenance: CodeProvenance,
    feature_service_name: str,
    training_attempt_id: str | None,
) -> str:
    """Log one identity binding code, data, model, and the parent attempt."""
    bundle_body: dict[str, object] = {
        "training_attempt_id": training_attempt_id,
        "mlflow_run_id": run_id,
        "model_uri": model_uri,
        "model_artifact_sha256": model_artifact_sha256,
        "dataset_sha256": dataset_sha256,
        "dataset_lineage_id": lineage_id,
        "code_sha256": code_provenance["code_sha256"],
        "git_commit": code_provenance["git_commit"],
        "git_dirty": code_provenance["git_dirty"],
        "git_state_available": code_provenance["git_state_available"],
        "feature_service_name": feature_service_name,
    }
    canonical = json.dumps(bundle_body, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    bundle_id = hashlib.sha256(canonical).hexdigest()
    mlflow.set_tag("release_bundle_id", bundle_id)
    mlflow.set_tag("model_artifact_sha256", model_artifact_sha256)
    mlflow.log_dict(
        {"release_bundle_id": bundle_id, **bundle_body},
        "provenance/release_bundle.json",
    )
    if training_attempt_id is not None:
        client = MlflowClient()
        for key, value in {
            "release_bundle_id": bundle_id,
            "model_run_id": run_id,
            "code_sha256": code_provenance["code_sha256"],
            "dataset_sha256": dataset_sha256,
            "model_artifact_sha256": model_artifact_sha256,
        }.items():
            client.set_tag(training_attempt_id, key, value)
    return bundle_id


def train_baseline(y_train: pd.Series) -> DummyRegressor:
    """Fit a median-price baseline for direct experiment comparison."""
    LOGGER.info("Fitting median-price baseline")
    baseline = DummyRegressor(strategy="median")
    baseline.fit(np.zeros((len(y_train), 1)), y_train)
    return baseline


def log_model_run(
    model: BaseEstimator,
    x_train: pd.DataFrame,
    x_test: pd.DataFrame,
    metrics: dict[str, float],
    *,
    run_name: str,
    model_family: str,
    dataset_version: str,
    git_sha: str,
    feature_service_name: str,
    parameters: dict[str, object],
    evaluation_report: dict[str, object],
    baseline: bool = False,
    lineage_manifest_path: str | Path | None = None,
    training_attempt_id: str | None = None,
    training_duration_seconds: float | None = None,
) -> dict[str, object]:
    """Log a fitted model and provenance, then verify artifact reload."""
    from training.provenance import sha256_directory

    with mlflow.start_run(run_name=run_name) as run:
        tags = {
            "model_family": model_family,
            "baseline": str(baseline).lower(),
            "git_sha": git_sha,
            "mlflow.source.git.commit": git_sha,
            "artifact_reload_verified": "pending",
        }
        if training_attempt_id is not None:
            tags["mlflow.parentRunId"] = training_attempt_id
            tags["training_attempt_id"] = training_attempt_id
        mlflow.set_tags(tags)
        mlflow.log_params(
            {
                **parameters,
                "dataset_sha256": dataset_version,
                "dataset_version": dataset_version,
                "feature_service_name": feature_service_name,
            }
        )
        mlflow.log_metrics(metrics)
        if training_duration_seconds is not None:
            if training_duration_seconds < 0:
                LOGGER.error("Model training duration cannot be negative")
                raise ValueError("training_duration_seconds must be non-negative")
            mlflow.log_metric("training_duration_seconds", training_duration_seconds)
            mlflow.log_param("training_duration_seconds", training_duration_seconds)
        code_provenance = _log_code_provenance()
        lineage_id = _log_lineage_manifest(lineage_manifest_path, dataset_version)
        sample = x_train.iloc[: min(3, len(x_train))]
        expected = model.predict(x_test.iloc[: min(32, len(x_test))])
        model_info = mlflow.sklearn.log_model(
            sk_model=model,
            name="model",
            serialization_format="cloudpickle",
            signature=infer_signature(sample, model.predict(sample)),
        )
        loaded = mlflow.sklearn.load_model(model_info.model_uri)
        np.testing.assert_allclose(
            loaded.predict(x_test.iloc[: min(32, len(x_test))]),
            expected,
            rtol=1e-10,
            atol=1e-10,
        )
        downloaded_model = mlflow.artifacts.download_artifacts(
            artifact_uri=model_info.model_uri
        )
        model_artifact_sha256 = sha256_directory(downloaded_model)
        bundle_id = _log_release_bundle(
            run_id=run.info.run_id,
            model_uri=model_info.model_uri,
            model_artifact_sha256=model_artifact_sha256,
            dataset_sha256=dataset_version,
            lineage_id=lineage_id,
            code_provenance=code_provenance,
            feature_service_name=feature_service_name,
            training_attempt_id=training_attempt_id,
        )
        mlflow.set_tag("artifact_reload_verified", "true")
        report: dict[str, object] = {
            **evaluation_report,
            "run_id": run.info.run_id,
            "model_uri": model_info.model_uri,
            "metrics": metrics,
            "artifact_reload_verified": True,
            "dataset_lineage_id": lineage_id,
            "code_sha256": code_provenance["code_sha256"],
            "model_artifact_sha256": model_artifact_sha256,
            "release_bundle_id": bundle_id,
        }
        mlflow.log_dict(report, "evaluation_report.json")
        return report


def train_and_log_champion(
    model: CatBoostPriceEnsembleRegressor,
    x_train: pd.DataFrame,
    x_test: pd.DataFrame,
    y_train: pd.Series,
    y_test: pd.Series,
    *,
    dataset_version: str,
    source_row_count: int,
    lineage_manifest_path: str | Path | None = None,
    training_attempt_id: str | None = None,
) -> dict[str, object]:
    """Fit, evaluate, log, and reload-verify the champion model."""
    from training.provenance import sha256_directory

    model.fit(x_train, y_train)
    predictions = model.predict(x_test)
    metrics = evaluate_segments(x_test, y_test, predictions)
    LOGGER.info("Champion metrics: %s", metrics)
    with mlflow.start_run(run_name="catboost_price_ensemble_champion") as run:
        tags = {
            "champion": "true",
            "promotion_status": "champion",
            "model_family": "catboost_ensemble",
            "artifact_reload_verified": "pending",
        }
        if training_attempt_id is not None:
            tags["mlflow.parentRunId"] = training_attempt_id
            tags["training_attempt_id"] = training_attempt_id
        mlflow.set_tags(tags)
        mlflow.log_params(
            {
                "model_name": MODEL_NAME,
                "model_class": type(model).__name__,
                "feature_service_name": FEATURE_SERVICE_NAME,
                "number_of_features": x_train.shape[1],
                "dataset_version": dataset_version,
                "dataset_sha256": dataset_version,
                "source_row_count": source_row_count,
                "training_row_count": len(x_train),
                "test_row_count": len(x_test),
                "test_size": TEST_SIZE,
                "random_states": model.random_states,
                "member_iterations": model.iterations,
                "learning_rate": model.learning_rate,
                "depth": model.depth,
                "l2_leaf_reg": model.l2_leaf_reg,
                "target_transform": "log1p",
                "aggregation": "mean dollar prediction",
            }
        )
        mlflow.log_metrics(metrics)
        code_provenance = _log_code_provenance()
        lineage_id = _log_lineage_manifest(lineage_manifest_path, dataset_version)
        mlflow.log_dict(model.feature_importances(), "feature_importance.json")
        sample = x_train.iloc[:3]
        model_info = mlflow.sklearn.log_model(
            sk_model=model,
            name="model",
            serialization_format="cloudpickle",
            signature=infer_signature(sample, model.predict(sample)),
        )
        loaded = mlflow.sklearn.load_model(model_info.model_uri)
        np.testing.assert_allclose(
            loaded.predict(x_test.iloc[:32]), predictions[:32], rtol=1e-10, atol=1e-10
        )
        downloaded_model = mlflow.artifacts.download_artifacts(
            artifact_uri=model_info.model_uri
        )
        model_artifact_sha256 = sha256_directory(downloaded_model)
        bundle_id = _log_release_bundle(
            run_id=run.info.run_id,
            model_uri=model_info.model_uri,
            model_artifact_sha256=model_artifact_sha256,
            dataset_sha256=dataset_version,
            lineage_id=lineage_id,
            code_provenance=code_provenance,
            feature_service_name=FEATURE_SERVICE_NAME,
            training_attempt_id=training_attempt_id,
        )
        mlflow.set_tag("artifact_reload_verified", "true")
        report: dict[str, object] = {
            "run_id": run.info.run_id,
            "model_uri": model_info.model_uri,
            "metrics": metrics,
            "dataset_lineage_id": lineage_id,
            "code_sha256": code_provenance["code_sha256"],
            "model_artifact_sha256": model_artifact_sha256,
            "release_bundle_id": bundle_id,
        }
        mlflow.log_dict(report, "report.json")
        return report


def run_training(
    data_path: Path = DEFAULT_DATA_PATH,
    feature_repo_path: Path = FEATURE_REPO_PATH,
    experiment_name: str = EXPERIMENT_NAME,
) -> dict[str, object]:
    """Run the reproducible Feast-to-MLflow champion pipeline."""
    from training.lineage import publish_dataset_snapshot, verify_dataset_snapshot
    from training.validation import require_valid_dataset

    tracking_uri = os.getenv(
        "MLFLOW_TRACKING_URI", f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"
    )
    attempt_id = create_training_attempt(
        tracking_uri, experiment_name, "airbnb_local_training"
    )
    try:
        resolved_data_path = data_path.expanduser().resolve()
        validation_report = require_valid_dataset(resolved_data_path)
        update_training_attempt(tracking_uri, attempt_id, "validated")
        configured_source = (
            Path(os.getenv("AIRBNB_RAW_DATA_PATH", DEFAULT_RAW_DATA_PATH))
            .expanduser()
            .resolve()
        )
        source_path = (
            configured_source if configured_source.is_file() else resolved_data_path
        )
        source_role = "raw_input" if configured_source.is_file() else "training_input"
        snapshot_root = (
            Path(os.getenv("AIRBNB_SNAPSHOT_ROOT", DEFAULT_SNAPSHOT_ROOT))
            .expanduser()
            .resolve()
        )
        published = publish_dataset_snapshot(
            resolved_data_path,
            snapshot_root,
            source_path=source_path,
            validation_report=validation_report,
            transformation_name="training.preprocessing.pipeline.run_pipeline",
            git_commit=get_git_sha(),
            feature_service_name=FEATURE_SERVICE_NAME,
            source_role=source_role,
            object_store_uri=os.getenv("AIRBNB_SNAPSHOT_S3_URI"),
        )
        immutable_data_path = Path(published["snapshot_path"])
        verify_dataset_snapshot(immutable_data_path, published["dataset_sha256"])
        if get_dataset_version(resolved_data_path) != published["dataset_sha256"]:
            LOGGER.error("Mutable Feast source changed after snapshot publication")
            raise ValueError(
                "Feast source changed after the training snapshot was created"
            )
        labels = load_labels(immutable_data_path)
        historical, feature_names = load_features_from_feast(
            feature_repo_path, labels.loc[:, list(ENTITY_COLUMNS)]
        )
        if get_dataset_version(resolved_data_path) != published["dataset_sha256"]:
            LOGGER.error("Mutable Feast source changed during historical retrieval")
            raise ValueError("Feast source changed during historical feature retrieval")
        update_training_attempt(tracking_uri, attempt_id, "features_retrieved")
        x, y = build_training_dataset(labels, historical, feature_names)
        x_train, x_test, y_train, y_test = train_test_split(
            x, y, test_size=TEST_SIZE, random_state=RANDOM_STATE
        )
        configured_manifest = os.getenv("AIRBNB_LINEAGE_MANIFEST_PATH")
        lineage_manifest = (
            Path(configured_manifest or published["lineage_manifest_path"])
            .expanduser()
            .resolve()
        )
        report = train_and_log_champion(
            CatBoostPriceEnsembleRegressor(tuple(feature_names)),
            x_train,
            x_test,
            y_train,
            y_test,
            dataset_version=published["dataset_sha256"],
            source_row_count=len(labels),
            lineage_manifest_path=lineage_manifest,
            training_attempt_id=attempt_id,
        )
        update_training_attempt(
            tracking_uri, attempt_id, "model_logged", outcome="completed"
        )
        return {**report, "training_attempt_id": attempt_id}
    except BaseException as exc:
        update_training_attempt(
            tracking_uri, attempt_id, "failed", outcome="failed", error=exc
        )
        raise


def _parse_args() -> argparse.Namespace:
    """Parse local training command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path(os.getenv("AIRBNB_FEATURE_DATA_PATH", DEFAULT_DATA_PATH)),
    )
    parser.add_argument("--feature-repo-path", type=Path, default=FEATURE_REPO_PATH)
    parser.add_argument(
        "--experiment-name",
        default=os.getenv("MLFLOW_EXPERIMENT_NAME", EXPERIMENT_NAME),
    )
    return parser.parse_args()


def main() -> None:
    """Configure logging and train the champion model."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parse_args()
    report = run_training(
        data_path=arguments.data_path.expanduser().resolve(),
        feature_repo_path=arguments.feature_repo_path.expanduser().resolve(),
        experiment_name=arguments.experiment_name,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
