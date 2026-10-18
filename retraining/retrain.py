"""Event-triggered Airflow DAG for retraining, approval, promotion, and rollback."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import mlflow
import mlflow.sklearn
from airflow.providers.standard.operators.hitl import ApprovalOperator
from airflow.sdk import DAG, Asset, dag, task
from mlflow import MlflowClient
from sklearn.model_selection import train_test_split

from monitoring.operational_metrics import (
    pipeline_failure,
    pipeline_success,
    publish_model_event,
    publish_offline_evaluation,
)
from training.deployment import (
    begin_promotion,
    complete_promotion,
    rollback_promotion_verified,
    verify_serving_version,
)
from training.lineage import publish_dataset_snapshot, verify_dataset_snapshot
from training.registry import tag_version_before_alias
from training.train import (
    ENTITY_COLUMNS,
    EXPERIMENT_NAME,
    FEATURE_REPO_PATH,
    FEATURE_SERVICE_NAME,
    RANDOM_STATE,
    TEST_SIZE,
    CatBoostPriceEnsembleRegressor,
    build_training_dataset,
    create_training_attempt,
    evaluate_segments,
    get_git_sha,
    load_features_from_feast,
    load_labels,
    log_model_run,
    update_training_attempt,
)
from training.validation import require_valid_dataset

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETRAINING_EVENT_ASSET = Asset("airbnb_retraining_event")
EVENT_DIR = (
    Path(
        os.getenv(
            "RETRAINING_EVENT_DIR", PROJECT_ROOT / "monitoring" / "retraining-events"
        )
    )
    .expanduser()
    .resolve()
)
WORK_DIR = Path(os.getenv("AIRBNB_RETRAINING_WORK_DIR", "/tmp/airbnb-retraining"))
SNAPSHOT_ROOT = (
    Path(os.getenv("AIRBNB_SNAPSHOT_ROOT", PROJECT_ROOT / "data" / "snapshots"))
    .expanduser()
    .resolve()
)
MLFLOW_TRACKING_URI = os.getenv(
    "MLFLOW_TRACKING_URI", f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"
)
MODEL_NAME = os.getenv("MODEL_NAME", "airbnb-price-predictor")
PRODUCTION_ALIAS = os.getenv("MODEL_ALIAS", "champion")
CHALLENGER_ALIAS = os.getenv("CHALLENGER_ALIAS", "challenger")
ROLLBACK_ALIAS = os.getenv("ROLLBACK_ALIAS", "rollback")
MINIMUM_MAE_IMPROVEMENT = float(os.getenv("MINIMUM_MAE_IMPROVEMENT", "2.0"))
MAXIMUM_RMSE_REGRESSION = float(os.getenv("MAXIMUM_RMSE_REGRESSION", "0.02"))
MAXIMUM_SEGMENT_MAE_REGRESSION = float(
    os.getenv("MAXIMUM_SEGMENT_MAE_REGRESSION", "0.05")
)
KEY_SEGMENT_METRICS: tuple[str, ...] = (
    "mae_entire_home",
    "mae_private_room",
    "mae_ordinary_market",
)
SERVING_URL = os.getenv("SERVING_URL", "http://airbnb-price-predictor")
POST_PROMOTION_P95_SECONDS = float(os.getenv("POST_PROMOTION_P95_SECONDS", "0.25"))
POST_PROMOTION_ATTEMPTS = int(os.getenv("POST_PROMOTION_ATTEMPTS", "12"))


class PromotionGateResult(TypedDict):
    """Machine-readable challenger comparison decision."""

    passed: bool
    rejection_reasons: list[str]
    mae_improvement: float


class ChallengerEvaluation(TypedDict):
    """Serializable result of identical-holdout champion comparison."""

    event: dict[str, object]
    run_id: str
    model_uri: str
    champion_version: str
    champion_metrics: dict[str, float]
    challenger_metrics: dict[str, float]
    gate: PromotionGateResult
    training_attempt_id: str
    release_bundle_id: str
    code_sha256: str
    dataset_sha256: str
    model_artifact_sha256: str


class RegisteredChallenger(ChallengerEvaluation):
    """Evaluation augmented with a non-production registry version."""

    challenger_version: str


class PromotionResult(TypedDict):
    """Audit data for one approved alias promotion."""

    model_name: str
    previous_version: str
    promoted_version: str
    approved_by: str
    approved_at: str
    operation_id: str
    status: str


class DeploymentCheckResult(TypedDict):
    """Post-promotion serving verification result."""

    passed: bool
    reasons: list[str]
    observed_version: str | None
    p95_latency_seconds: float | None


def evaluate_promotion_gates(
    champion_metrics: dict[str, float],
    challenger_metrics: dict[str, float],
    *,
    minimum_mae_improvement: float = MINIMUM_MAE_IMPROVEMENT,
    maximum_rmse_regression: float = MAXIMUM_RMSE_REGRESSION,
    maximum_segment_mae_regression: float = MAXIMUM_SEGMENT_MAE_REGRESSION,
    segment_metrics: tuple[str, ...] = KEY_SEGMENT_METRICS,
) -> PromotionGateResult:
    """Apply MAE improvement and RMSE/segment non-regression guardrails."""
    required = {"mae", "rmse", *segment_metrics}
    missing = sorted(
        required.difference(champion_metrics) | required.difference(challenger_metrics)
    )
    if missing:
        return {
            "passed": False,
            "rejection_reasons": [f"missing comparison metrics: {missing}"],
            "mae_improvement": float("nan"),
        }
    if any(
        not math.isfinite(metrics[key])
        for metrics in (champion_metrics, challenger_metrics)
        for key in required
    ):
        LOGGER.warning("Non-finite offline comparison metrics block promotion")
        return {
            "passed": False,
            "rejection_reasons": ["non-finite comparison metrics"],
            "mae_improvement": float("nan"),
        }
    improvement = champion_metrics["mae"] - challenger_metrics["mae"]
    reasons: list[str] = []
    if improvement < minimum_mae_improvement:
        reasons.append(
            f"MAE improvement {improvement:.6f} is below {minimum_mae_improvement:.6f}"
        )
    rmse_limit = champion_metrics["rmse"] * (1.0 + maximum_rmse_regression)
    if challenger_metrics["rmse"] > rmse_limit:
        reasons.append(
            f"RMSE {challenger_metrics['rmse']:.6f} exceeds guardrail {rmse_limit:.6f}"
        )
    for metric in segment_metrics:
        limit = champion_metrics[metric] * (1.0 + maximum_segment_mae_regression)
        if challenger_metrics[metric] > limit:
            reasons.append(
                f"{metric} {challenger_metrics[metric]:.6f} exceeds guardrail {limit:.6f}"
            )
    return {
        "passed": not reasons,
        "rejection_reasons": reasons,
        "mae_improvement": improvement,
    }


def require_approval(approval_response: object) -> tuple[str, str]:
    """Return approver identity and timestamp or reject missing/non-approval input."""
    if not isinstance(approval_response, dict):
        raise TypeError("A recorded human approval is required before promotion")
    options = approval_response.get("chosen_options")
    approved_by = approval_response.get("responded_by_user")
    approved_at = approval_response.get("responded_at")
    if options != ["Approve"] or not approved_by or not approved_at:
        raise ValueError("A recorded human approval is required before promotion")
    return str(approved_by), str(approved_at)


def promote_registered_challenger(
    client: Any,
    model_name: str,
    production_alias: str,
    rollback_alias: str,
    challenger_version: str,
    approval_response: object,
    expected_previous: str = "",
) -> PromotionResult:
    """Validate approved artifacts and save an interruption-safe promotion receipt."""
    approved_by, approved_at = require_approval(approval_response)
    operation = hashlib.sha256(
        f"{model_name}:{challenger_version}:{approved_by}:{approved_at}".encode()
    ).hexdigest()
    return begin_promotion(
        client,
        model_name,
        challenger_version,
        operation_id=operation,
        approved_by=approved_by,
        approved_at=approved_at,
        production_alias=production_alias,
        rollback_alias=rollback_alias,
        expected_previous=expected_previous,
    )  # type: ignore[return-value]


def restore_previous_alias(
    client: Any,
    promotion: PromotionResult,
    production_alias: str,
    reason: str = "post-promotion serving validation failed",
) -> str:
    """Restore the saved production model and verify its actual serving predictions."""
    return rollback_promotion_verified(
        client,
        dict(promotion),
        production_alias=production_alias,
        reason=reason,
        verifier=verify_serving_version,
    )


def deployment_checks_pass(result: DeploymentCheckResult) -> bool:
    """Return whether every post-promotion serving check succeeded."""
    return result["passed"] and not result["reasons"]


def _write_pickle(path: Path, payload: object) -> None:
    """Persist a trusted internal retraining artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as artifact_file:
        pickle.dump(payload, artifact_file)


def _latest_event() -> dict[str, object]:
    """Load the newest durable monitoring event for reproducible metadata."""
    candidates = sorted(EVENT_DIR.glob("retrain-*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No retraining event found in {EVENT_DIR}")
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Retraining event must be a JSON object")
    return payload


@task(retries=0)
def load_retraining_event() -> dict[str, object]:
    """Load and validate feature/data-window metadata from the triggering event."""
    event = _latest_event()
    required = {
        "model_version",
        "production_window_rows",
        "production_path",
        "report_path",
    }
    missing = sorted(required.difference(event))
    if missing:
        raise ValueError(f"Retraining event is missing metadata: {missing}")
    return event


@task(retries=0)
def train_and_compare(event: dict[str, object]) -> ChallengerEvaluation:
    """Evaluate champion and challenger on one identical deterministic holdout."""
    experiment_name = os.getenv("MLFLOW_EXPERIMENT_NAME", EXPERIMENT_NAME)
    attempt_id = create_training_attempt(
        MLFLOW_TRACKING_URI, experiment_name, "airbnb_model_retraining"
    )
    try:
        mutable_data_path = Path(str(event["production_path"])).expanduser().resolve()
        validation_report = require_valid_dataset(mutable_data_path)
        published = publish_dataset_snapshot(
            mutable_data_path,
            SNAPSHOT_ROOT,
            source_path=mutable_data_path,
            validation_report=validation_report,
            transformation_name="monitoring.production_training_window",
            git_commit=get_git_sha(),
            feature_service_name=FEATURE_SERVICE_NAME,
            source_role="production_training_window",
        )
        data_path = Path(published["snapshot_path"])
        verify_dataset_snapshot(data_path, published["dataset_sha256"])
        update_training_attempt(MLFLOW_TRACKING_URI, attempt_id, "validated")
        labels = load_labels(data_path)
        historical, feature_names = load_features_from_feast(
            FEATURE_REPO_PATH,
            labels.loc[:, list(ENTITY_COLUMNS)],
            FEATURE_SERVICE_NAME,
        )
        features, target = build_training_dataset(labels, historical, feature_names)
        x_train, x_test, y_train, y_test = train_test_split(
            features, target, test_size=TEST_SIZE, random_state=RANDOM_STATE
        )
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        client = MlflowClient()
        champion_version = str(
            client.get_model_version_by_alias(MODEL_NAME, PRODUCTION_ALIAS).version
        )
        champion = mlflow.sklearn.load_model(f"models:/{MODEL_NAME}/{champion_version}")
        challenger = CatBoostPriceEnsembleRegressor(tuple(feature_names))
        challenger.fit(x_train, y_train)
        champion_metrics = evaluate_segments(x_test, y_test, champion.predict(x_test))
        challenger_metrics = evaluate_segments(
            x_test, y_test, challenger.predict(x_test)
        )
        gate = evaluate_promotion_gates(champion_metrics, challenger_metrics)
        publish_offline_evaluation("champion", champion_metrics)
        publish_offline_evaluation("challenger", challenger_metrics)
        publish_model_event("retraining", attempt_id)
        mlflow.set_experiment(experiment_name)
        logged = log_model_run(
            challenger,
            x_train,
            x_test,
            challenger_metrics,
            run_name="drift_retraining_challenger",
            model_family="catboost_ensemble",
            dataset_version=published["dataset_sha256"],
            git_sha=get_git_sha(),
            feature_service_name=FEATURE_SERVICE_NAME,
            parameters={
                "trigger_model_version": event["model_version"],
                "production_window_rows": event["production_window_rows"],
                "production_path": event["production_path"],
                "drift_report_path": event["report_path"],
                "holdout_random_state": RANDOM_STATE,
                "holdout_test_size": TEST_SIZE,
            },
            evaluation_report={
                "trigger_event": event,
                "champion_version": champion_version,
                "champion_metrics": champion_metrics,
                "challenger_metrics": challenger_metrics,
                "promotion_gate": gate,
            },
            lineage_manifest_path=published["lineage_manifest_path"],
            training_attempt_id=attempt_id,
        )
        update_training_attempt(MLFLOW_TRACKING_URI, attempt_id, "model_logged")
        _write_pickle(WORK_DIR / "holdout.pkl", {"x_test": x_test, "y_test": y_test})
        return {
            "event": event,
            "run_id": str(logged["run_id"]),
            "model_uri": str(logged["model_uri"]),
            "champion_version": champion_version,
            "champion_metrics": champion_metrics,
            "challenger_metrics": challenger_metrics,
            "gate": gate,
            "training_attempt_id": attempt_id,
            "release_bundle_id": str(logged["release_bundle_id"]),
            "code_sha256": str(logged["code_sha256"]),
            "dataset_sha256": published["dataset_sha256"],
            "model_artifact_sha256": str(logged["model_artifact_sha256"]),
        }
    except BaseException as exc:
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            attempt_id,
            "retraining_failed",
            outcome="failed",
            error=exc,
        )
        raise


@task.branch
def challenger_gate(evaluation: ChallengerEvaluation) -> str:
    """Route passing challengers to registration and failures to rejection."""
    return "register_challenger" if evaluation["gate"]["passed"] else "record_rejection"


@task
def record_rejection(evaluation: ChallengerEvaluation) -> list[str]:
    """Retain the champion and persist explicit rejection reasons in MLflow."""
    try:
        reasons = evaluation["gate"]["rejection_reasons"]
        client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)
        client.set_tag(evaluation["run_id"], "promotion_status", "rejected")
        client.set_tag(evaluation["run_id"], "rejection_reasons", json.dumps(reasons))
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            evaluation["training_attempt_id"],
            "challenger_gate",
            outcome="rejected",
        )
        LOGGER.warning("Challenger rejected; champion retained: %s", reasons)
        return reasons
    except BaseException as exc:
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            evaluation["training_attempt_id"],
            "rejection_recording_failed",
            outcome="failed",
            error=exc,
        )
        raise


@task(retries=1)
def register_challenger(evaluation: ChallengerEvaluation) -> RegisteredChallenger:
    """Register a passing challenger without moving the production alias."""
    try:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        version = mlflow.register_model(evaluation["model_uri"], MODEL_NAME)
        client = MlflowClient()
        provenance_tags = {
            "promotion_status": "awaiting_approval",
            "release_bundle_id": evaluation["release_bundle_id"],
            "training_attempt_id": evaluation["training_attempt_id"],
            "mlflow_run_id": evaluation["run_id"],
            "code_sha256": evaluation["code_sha256"],
            "dataset_sha256": evaluation["dataset_sha256"],
            "model_artifact_sha256": evaluation["model_artifact_sha256"],
        }
        tag_version_before_alias(
            client,
            MODEL_NAME,
            str(version.version),
            CHALLENGER_ALIAS,
            provenance_tags,
        )
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            evaluation["training_attempt_id"],
            "challenger_registered",
            outcome="registered",
        )
        return {**evaluation, "challenger_version": str(version.version)}
    except BaseException as exc:
        update_training_attempt(
            MLFLOW_TRACKING_URI,
            evaluation["training_attempt_id"],
            "challenger_registration_failed",
            outcome="failed",
            error=exc,
        )
        raise


@task(retries=0)
def promote_to_production(
    challenger: RegisteredChallenger, approval_response: object
) -> PromotionResult:
    """Audit approval, preserve rollback state, and move the production alias."""
    client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)
    return promote_registered_challenger(
        client,
        MODEL_NAME,
        PRODUCTION_ALIAS,
        ROLLBACK_ALIAS,
        challenger["challenger_version"],
        approval_response,
        expected_previous=challenger["champion_version"],
    )


@task(retries=0)
def run_post_promotion_checks(promotion: PromotionResult) -> DeploymentCheckResult:
    """Verify readiness, concrete predictions and bounded p95 using the shared checker."""
    try:
        if promotion["status"] == "rolled_back":
            raise RuntimeError("Interrupted operation already rolled back")
        verify_serving_version(promotion["promoted_version"])
        return {
            "passed": True,
            "reasons": [],
            "observed_version": promotion["promoted_version"],
            "p95_latency_seconds": None,
        }
    except RuntimeError as exc:
        LOGGER.warning(
            "Post-promotion serving validation failed: %s", type(exc).__name__
        )
        return {
            "passed": False,
            "reasons": [str(exc)],
            "observed_version": None,
            "p95_latency_seconds": None,
        }


@task.branch
def deployment_gate(result: DeploymentCheckResult) -> str:
    """Route failed deployment checks to automatic rollback."""
    return (
        "promotion_succeeded"
        if deployment_checks_pass(result)
        else "rollback_promotion"
    )


@task(retries=1)
def rollback_promotion(
    promotion: PromotionResult, checks: DeploymentCheckResult
) -> str:
    """Automatically restore the saved production alias target."""
    client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)
    restored = restore_previous_alias(
        client, promotion, PRODUCTION_ALIAS, "; ".join(checks["reasons"])
    )
    LOGGER.error("Promotion rolled back to model version %s", restored)
    return restored


@task
def promotion_succeeded(promotion: PromotionResult) -> None:
    """Record successful completion of approved production promotion."""
    complete_promotion(
        MlflowClient(tracking_uri=MLFLOW_TRACKING_URI),
        dict(promotion),
        serving_verified=True,
    )
    LOGGER.info(
        "Promotion completed: %s -> %s",
        promotion["previous_version"],
        promotion["promoted_version"],
    )


@dag(
    dag_id="airbnb_model_retraining",
    description="Retrain from drift events with approval, checks, and rollback.",
    schedule=[RETRAINING_EVENT_ASSET],
    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    on_success_callback=pipeline_success,
    on_failure_callback=pipeline_failure,
    max_active_runs=1,
    tags=["airbnb", "retraining", "approval", "mlflow"],
)
def retraining_pipeline() -> None:
    """Define the governed challenger-to-production lifecycle."""
    event = load_retraining_event()
    training_approval = ApprovalOperator(
        task_id="approve_expensive_training",
        subject="Approve expensive drift-driven training",
        body="Review the drift event and compute budget before starting the ensemble.",
        fail_on_reject=True,
    )
    event >> training_approval
    evaluation = train_and_compare(event)  # type: ignore[arg-type]
    training_approval >> evaluation
    gate = challenger_gate(evaluation)  # type: ignore[arg-type]
    rejected = record_rejection(evaluation)  # type: ignore[arg-type]
    registered = register_challenger(evaluation)  # type: ignore[arg-type]
    gate >> [rejected, registered]
    approval = ApprovalOperator(
        task_id="approve_production_promotion",
        subject="Approve Airbnb model production promotion",
        body=(
            "Review the challenger MLflow run, identical-holdout metrics, guardrails, "
            "and model version before selecting Approve or Reject."
        ),
        fail_on_reject=False,
    )
    registered >> approval
    promotion = promote_to_production(
        registered,
        approval.output,  # type: ignore[arg-type]
    )
    checks = run_post_promotion_checks(promotion)  # type: ignore[arg-type]
    post_gate = deployment_gate(checks)  # type: ignore[arg-type]
    rolled_back = rollback_promotion(promotion, checks)  # type: ignore[arg-type]
    succeeded = promotion_succeeded(promotion)  # type: ignore[arg-type]
    post_gate >> [rolled_back, succeeded]


retraining_dag: DAG = retraining_pipeline()
