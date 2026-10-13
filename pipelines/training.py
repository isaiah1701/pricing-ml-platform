"""Airflow DAG for validated Feast retrieval, training, tracking, and registration."""

from __future__ import annotations

import json
import logging
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import mlflow
import pandas as pd
from airflow.sdk import DAG, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from mlflow import MlflowClient
from sklearn.model_selection import train_test_split

from training.lineage import publish_dataset_snapshot, verify_dataset_snapshot
from training.registry import tag_version_before_alias
from training.train import (
    ENTITY_COLUMNS,
    EXPERIMENT_NAME,
    FEATURE_SERVICE_NAME,
    RANDOM_STATE,
    TEST_SIZE,
    CatBoostPriceEnsembleRegressor,
    build_training_dataset,
    create_training_attempt,
    evaluate_segments,
    get_dataset_version,
    get_git_sha,
    load_features_from_feast,
    load_labels,
    log_model_run,
    train_baseline,
    update_training_attempt,
)
from training.validation import ValidationReport, require_valid_dataset

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO_PATH = Path(
    os.getenv("AIRBNB_FEATURE_REPO_PATH", PROJECT_ROOT / "feature_repo")
).expanduser().resolve()
DATA_PATH = Path(
    os.getenv(
        "AIRBNB_FEATURE_DATA_PATH", PROJECT_ROOT / "data" / "listing_features.parquet"
    )
).expanduser().resolve()
RAW_DATA_PATH = Path(
    os.getenv("AIRBNB_RAW_DATA_PATH", PROJECT_ROOT / "data" / "AB_NYC_2019.csv")
).expanduser().resolve()
SNAPSHOT_ROOT = Path(
    os.getenv("AIRBNB_SNAPSHOT_ROOT", PROJECT_ROOT / "data" / "snapshots")
).expanduser().resolve()
WORK_DIR = Path(os.getenv("AIRBNB_TRAINING_WORK_DIR", "/tmp/airbnb-training"))
MLFLOW_TRACKING_URI = os.getenv(
    "MLFLOW_TRACKING_URI", f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"
)
MAE_THRESHOLD = float(os.getenv("MODEL_REGISTRATION_MAE_THRESHOLD", "75.0"))
REGISTERED_MODEL_NAME = os.getenv("MODEL_NAME", "airbnb-price-predictor")
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "champion")


class RetrievalResult(TypedDict):
    """Serializable description of the point-in-time training dataset."""

    artifact_path: str
    dataset_sha256: str
    feature_service_name: str
    row_count: int
    validation_report: ValidationReport
    lineage_id: str
    lineage_manifest_path: str
    training_attempt_id: str


class ValidatedTrainingDataset(TypedDict):
    """Validated immutable dataset references passed into Feast retrieval."""

    snapshot_path: str
    dataset_sha256: str
    lineage_id: str
    lineage_manifest_path: str
    validation_report: ValidationReport
    training_attempt_id: str


class TrainingResult(TypedDict):
    """Serializable description of fitted baseline and candidate artifacts."""

    artifact_path: str
    dataset_sha256: str
    feature_service_name: str
    row_count: int
    validation_report: ValidationReport
    lineage_id: str
    lineage_manifest_path: str
    training_attempt_id: str


class EvaluationResult(TrainingResult):
    """Training result augmented with model evaluation metrics."""

    evaluation_path: str
    baseline_metrics: dict[str, float]
    candidate_metrics: dict[str, float]


class LoggedCandidate(TypedDict):
    """Registration inputs returned by MLflow logging."""

    run_id: str
    model_uri: str
    mae: float
    artifact_reload_verified: bool
    training_attempt_id: str
    release_bundle_id: str
    code_sha256: str
    dataset_sha256: str
    model_artifact_sha256: str


def registration_gate(
    candidate_mae: float,
    mae_threshold: float,
    artifact_reload_verified: bool,
) -> bool:
    """Return whether a candidate satisfies every registration requirement."""
    return artifact_reload_verified and candidate_mae < mae_threshold


def _write_pickle(path: Path, payload: object) -> None:
    """Persist an internal task artifact after creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as artifact_file:
        pickle.dump(payload, artifact_file)


def _read_pickle(path: Path) -> Any:
    """Load an internal task artifact created by this DAG."""
    with path.open("rb") as artifact_file:
        return pickle.load(artifact_file)


def mark_training_pipeline_failure(context: dict[str, Any]) -> None:
    """Terminate the parent MLflow attempt when an Airflow task fails."""
    task_instance = context.get("task_instance")
    if task_instance is None:
        LOGGER.error("Airflow failure callback did not receive a task instance")
        return
    validated = task_instance.xcom_pull(task_ids="validate_training_dataset")
    if not isinstance(validated, dict):
        return
    attempt_id = validated.get("training_attempt_id")
    if not isinstance(attempt_id, str):
        return
    failure = context.get("exception")
    error = failure if isinstance(failure, BaseException) else RuntimeError(
        "Airflow training task failed"
    )
    update_training_attempt(
        MLFLOW_TRACKING_URI,
        attempt_id,
        str(task_instance.task_id),
        outcome="failed",
        error=error,
    )


@task(retries=0)
def validate_training_dataset() -> ValidatedTrainingDataset:
    """Validate and freeze the exact dataset that the run will consume."""
    attempt_id = create_training_attempt(
        MLFLOW_TRACKING_URI,
        os.getenv("MLFLOW_EXPERIMENT_NAME", EXPERIMENT_NAME),
        "airbnb_model_training",
    )
    try:
        report = require_valid_dataset(DATA_PATH)
        source_path = RAW_DATA_PATH if RAW_DATA_PATH.is_file() else DATA_PATH
        published = publish_dataset_snapshot(
            DATA_PATH,
            SNAPSHOT_ROOT,
            source_path=source_path,
            validation_report=report,
            transformation_name="training.preprocessing.pipeline.run_pipeline",
            git_commit=get_git_sha(),
            feature_service_name=FEATURE_SERVICE_NAME,
            object_store_uri=os.getenv("AIRBNB_SNAPSHOT_S3_URI"),
        )
        immutable_report = require_valid_dataset(published["snapshot_path"])
        update_training_attempt(MLFLOW_TRACKING_URI, attempt_id, "validated")
        return {
            "snapshot_path": published["snapshot_path"],
            "dataset_sha256": published["dataset_sha256"],
            "lineage_id": published["lineage_id"],
            "lineage_manifest_path": published["lineage_manifest_path"],
            "validation_report": immutable_report,
            "training_attempt_id": attempt_id,
        }
    except BaseException as exc:
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            attempt_id,
            "validation_failed",
            outcome="failed",
            error=exc,
        )
        raise


@task(retries=1)
def retrieve_point_in_time_features(
    validated_dataset: ValidatedTrainingDataset,
) -> RetrievalResult:
    """Retrieve and align point-in-time-correct Feast features with labels."""
    update_training_attempt(
        MLFLOW_TRACKING_URI,
        validated_dataset["training_attempt_id"],
        "retrieving_features",
    )
    snapshot_path = Path(validated_dataset["snapshot_path"])
    verify_dataset_snapshot(snapshot_path, validated_dataset["dataset_sha256"])
    if get_dataset_version(DATA_PATH) != validated_dataset["dataset_sha256"]:
        LOGGER.error("Mutable Feast source changed after training snapshot creation")
        raise ValueError("Feast source changed after the training snapshot was created")
    labels = load_labels(snapshot_path)
    historical, feature_names = load_features_from_feast(
        FEATURE_REPO_PATH,
        labels.loc[:, list(ENTITY_COLUMNS)],
        FEATURE_SERVICE_NAME,
    )
    features, target = build_training_dataset(labels, historical, feature_names)
    if get_dataset_version(DATA_PATH) != validated_dataset["dataset_sha256"]:
        LOGGER.error("Mutable Feast source changed during historical retrieval")
        raise ValueError("Feast source changed during historical feature retrieval")
    update_training_attempt(
        MLFLOW_TRACKING_URI,
        validated_dataset["training_attempt_id"],
        "features_retrieved",
    )
    artifact_path = WORK_DIR / "retrieved.pkl"
    _write_pickle(
        artifact_path,
        {"features": features, "target": target, "feature_names": feature_names},
    )
    return {
        "artifact_path": str(artifact_path),
        "dataset_sha256": validated_dataset["dataset_sha256"],
        "feature_service_name": FEATURE_SERVICE_NAME,
        "row_count": len(labels),
        "validation_report": validated_dataset["validation_report"],
        "lineage_id": validated_dataset["lineage_id"],
        "lineage_manifest_path": validated_dataset["lineage_manifest_path"],
        "training_attempt_id": validated_dataset["training_attempt_id"],
    }


@task(retries=0)
def train_models(retrieval: RetrievalResult) -> TrainingResult:
    """Fit the median baseline and reusable CatBoost candidate."""
    update_training_attempt(
        MLFLOW_TRACKING_URI, retrieval["training_attempt_id"], "fitting_models"
    )
    retrieved = _read_pickle(Path(retrieval["artifact_path"]))
    x_train, x_test, y_train, y_test = train_test_split(
        retrieved["features"],
        retrieved["target"],
        test_size=TEST_SIZE,
        random_state=RANDOM_STATE,
    )
    baseline = train_baseline(y_train)
    candidate = CatBoostPriceEnsembleRegressor(tuple(retrieved["feature_names"]))
    candidate.fit(x_train, y_train)
    artifact_path = WORK_DIR / "trained.pkl"
    _write_pickle(
        artifact_path,
        {
            "x_train": x_train,
            "x_test": x_test,
            "y_train": y_train,
            "y_test": y_test,
            "baseline": baseline,
            "candidate": candidate,
        },
    )
    update_training_attempt(
        MLFLOW_TRACKING_URI, retrieval["training_attempt_id"], "models_fitted"
    )
    return {**retrieval, "artifact_path": str(artifact_path)}


@task(retries=0)
def evaluate_models(training_result: TrainingResult) -> EvaluationResult:
    """Evaluate the baseline and candidate on the same held-out rows."""
    update_training_attempt(
        MLFLOW_TRACKING_URI,
        training_result["training_attempt_id"],
        "evaluating_models",
    )
    trained = _read_pickle(Path(training_result["artifact_path"]))
    x_test: pd.DataFrame = trained["x_test"]
    y_test: pd.Series = trained["y_test"]
    baseline_metrics = evaluate_segments(
        x_test, y_test, trained["baseline"].predict(x_test)
    )
    candidate_metrics = evaluate_segments(
        x_test, y_test, trained["candidate"].predict(x_test)
    )
    evaluation_path = WORK_DIR / "evaluation.json"
    evaluation_path.parent.mkdir(parents=True, exist_ok=True)
    evaluation_path.write_text(
        json.dumps(
            {"baseline": baseline_metrics, "candidate": candidate_metrics}, indent=2
        ),
        encoding="utf-8",
    )
    update_training_attempt(
        MLFLOW_TRACKING_URI,
        training_result["training_attempt_id"],
        "models_evaluated",
    )
    return {
        **training_result,
        "evaluation_path": str(evaluation_path),
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
    }


@task(retries=1)
def log_models_to_mlflow(evaluation: EvaluationResult) -> LoggedCandidate:
    """Log comparable baseline and CatBoost runs to one MLflow experiment."""
    update_training_attempt(
        MLFLOW_TRACKING_URI, evaluation["training_attempt_id"], "logging_models"
    )
    trained = _read_pickle(Path(evaluation["artifact_path"]))
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT_NAME", EXPERIMENT_NAME))
    common_report: dict[str, object] = {
        "dataset_validation": evaluation["validation_report"],
        "evaluation_artifact": evaluation["evaluation_path"],
        "dataset_lineage_id": evaluation["lineage_id"],
    }
    git_sha = get_git_sha()
    log_model_run(
        trained["baseline"],
        trained["x_train"],
        trained["x_test"],
        evaluation["baseline_metrics"],
        run_name="median_price_baseline",
        model_family="dummy_median",
        dataset_version=evaluation["dataset_sha256"],
        git_sha=git_sha,
        feature_service_name=evaluation["feature_service_name"],
        parameters={"strategy": "median", "test_size": TEST_SIZE},
        evaluation_report={**common_report, "model_role": "baseline"},
        baseline=True,
        lineage_manifest_path=evaluation["lineage_manifest_path"],
        training_attempt_id=evaluation["training_attempt_id"],
    )
    candidate = trained["candidate"]
    report = log_model_run(
        candidate,
        trained["x_train"],
        trained["x_test"],
        evaluation["candidate_metrics"],
        run_name="catboost_price_ensemble_candidate",
        model_family="catboost_ensemble",
        dataset_version=evaluation["dataset_sha256"],
        git_sha=git_sha,
        feature_service_name=evaluation["feature_service_name"],
        parameters={
            "random_states": candidate.random_states,
            "member_iterations": candidate.iterations,
            "learning_rate": candidate.learning_rate,
            "depth": candidate.depth,
            "l2_leaf_reg": candidate.l2_leaf_reg,
            "test_size": TEST_SIZE,
        },
        evaluation_report={**common_report, "model_role": "candidate"},
        lineage_manifest_path=evaluation["lineage_manifest_path"],
        training_attempt_id=evaluation["training_attempt_id"],
    )
    update_training_attempt(
        MLFLOW_TRACKING_URI, evaluation["training_attempt_id"], "models_logged"
    )
    return {
        "run_id": str(report["run_id"]),
        "model_uri": str(report["model_uri"]),
        "mae": evaluation["candidate_metrics"]["mae"],
        "artifact_reload_verified": bool(report["artifact_reload_verified"]),
        "training_attempt_id": evaluation["training_attempt_id"],
        "release_bundle_id": str(report["release_bundle_id"]),
        "code_sha256": str(report["code_sha256"]),
        "dataset_sha256": evaluation["dataset_sha256"],
        "model_artifact_sha256": str(report["model_artifact_sha256"]),
    }


@task(retries=0)
def conditional_registration(candidate: LoggedCandidate) -> dict[str, str]:
    """Register only candidates that pass MAE and artifact-reload gates."""
    if not registration_gate(
        candidate["mae"], MAE_THRESHOLD, candidate["artifact_reload_verified"]
    ):
        LOGGER.info(
            "Skipping registration: mae=%f threshold=%f reload_verified=%s",
            candidate["mae"],
            MAE_THRESHOLD,
            candidate["artifact_reload_verified"],
        )
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            candidate["training_attempt_id"],
            "registration_gate",
            outcome="rejected",
        )
        raise AirflowSkipException("Candidate did not pass the registration gate")
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    version = mlflow.register_model(candidate["model_uri"], REGISTERED_MODEL_NAME)
    client = MlflowClient()
    provenance_tags = {
        "release_bundle_id": candidate["release_bundle_id"],
        "training_attempt_id": candidate["training_attempt_id"],
        "mlflow_run_id": candidate["run_id"],
        "code_sha256": candidate["code_sha256"],
        "dataset_sha256": candidate["dataset_sha256"],
        "model_artifact_sha256": candidate["model_artifact_sha256"],
    }
    tag_version_before_alias(
        client,
        REGISTERED_MODEL_NAME,
        str(version.version),
        MODEL_ALIAS,
        provenance_tags,
    )
    update_training_attempt(
        MLFLOW_TRACKING_URI,
        candidate["training_attempt_id"],
        "registered",
        outcome="registered",
    )
    LOGGER.info("Registered candidate as version %s", version.version)
    return {"name": REGISTERED_MODEL_NAME, "version": str(version.version)}


@dag(
    dag_id="airbnb_model_training",
    description="Validate, retrieve, train, evaluate, track, and gate Airbnb models.",
    schedule=None,
    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    on_failure_callback=mark_training_pipeline_failure,
    tags=["airbnb", "training", "feast", "mlflow"],
)
def training_pipeline() -> None:
    """Define the ordered model-training lifecycle."""
    validation = validate_training_dataset()
    retrieval = retrieve_point_in_time_features(validation)  # type: ignore[arg-type]
    trained = train_models(retrieval)  # type: ignore[arg-type]
    evaluated = evaluate_models(trained)  # type: ignore[arg-type]
    logged = log_models_to_mlflow(evaluated)  # type: ignore[arg-type]
    conditional_registration(logged)  # type: ignore[arg-type]


training_dag: DAG = training_pipeline()
