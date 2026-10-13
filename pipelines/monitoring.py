"""Scheduled Airflow DAG for drift analysis and thresholded retraining events."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from airflow.sdk import DAG, Asset, dag, task

from monitoring.drift_check import DriftResult, run_drift_check

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PATH = Path(
    os.getenv("DRIFT_REFERENCE_PATH", PROJECT_ROOT / "data" / "listing_features.parquet")
).expanduser().resolve()
PRODUCTION_PATH = Path(
    os.getenv("DRIFT_PRODUCTION_PATH", PROJECT_ROOT / "data" / "production_features.parquet")
).expanduser().resolve()
REPORT_DIR = Path(
    os.getenv("DRIFT_REPORT_DIR", PROJECT_ROOT / "monitoring" / "reports")
).expanduser().resolve()
METRICS_PATH = Path(
    os.getenv(
        "DRIFT_PROMETHEUS_METRICS_PATH",
        PROJECT_ROOT / "monitoring" / "metrics" / "drift.prom",
    )
).expanduser().resolve()
RETRAINING_EVENT_DIR = Path(
    os.getenv(
        "RETRAINING_EVENT_DIR", PROJECT_ROOT / "monitoring" / "retraining-events"
    )
).expanduser().resolve()
PRODUCTION_WINDOW_ROWS = int(os.getenv("DRIFT_PRODUCTION_WINDOW_ROWS", "10000"))
FEATURE_DRIFT_THRESHOLD = float(os.getenv("FEATURE_DRIFT_THRESHOLD", "0.2"))
RETRAINING_SHARE_THRESHOLD = float(
    os.getenv("DRIFT_RETRAINING_SHARE_THRESHOLD", "0.5")
)
MODEL_VERSION = os.getenv("MODEL_VERSION", "unknown")
RETRAINING_EVENT_ASSET = Asset("airbnb_retraining_event")


class RetrainingEvent(TypedDict):
    """Serializable event consumed by a downstream retraining workflow."""

    event_type: str
    emitted_at: str
    model_version: str
    drift_share: float
    drifted_feature_count: int
    production_window_rows: int
    production_path: str
    report_path: str


@task(retries=1)
def execute_drift_check() -> DriftResult:
    """Run the configured production-window drift comparison."""
    return run_drift_check(
        REFERENCE_PATH,
        PRODUCTION_PATH,
        REPORT_DIR,
        METRICS_PATH,
        production_window_rows=PRODUCTION_WINDOW_ROWS,
        drift_threshold=FEATURE_DRIFT_THRESHOLD,
        dataset_drift_share_threshold=RETRAINING_SHARE_THRESHOLD,
        model_version=MODEL_VERSION,
    )


@task.branch
def drift_threshold_gate(result: DriftResult) -> str:
    """Route to event emission only when the configured share is reached."""
    if result["drift_share"] >= RETRAINING_SHARE_THRESHOLD:
        LOGGER.warning(
            "Drift share %.3f reached retraining threshold %.3f",
            result["drift_share"],
            RETRAINING_SHARE_THRESHOLD,
        )
        return "emit_retraining_event"
    return "no_retraining_needed"


@task(retries=1, outlets=[RETRAINING_EVENT_ASSET])
def emit_retraining_event(result: DriftResult) -> str:
    """Atomically emit a durable event for the downstream retraining DAG."""
    emitted_at = datetime.now(timezone.utc)
    event: RetrainingEvent = {
        "event_type": "airbnb.model.retraining.requested",
        "emitted_at": emitted_at.isoformat(),
        "model_version": result["model_version"],
        "drift_share": result["drift_share"],
        "drifted_feature_count": result["drifted_feature_count"],
        "production_window_rows": result["production_window_rows"],
        "production_path": result["production_path"],
        "report_path": result["json_report_path"],
    }
    RETRAINING_EVENT_DIR.mkdir(parents=True, exist_ok=True)
    event_path = RETRAINING_EVENT_DIR / (
        f"retrain-{emitted_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=RETRAINING_EVENT_DIR,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        json.dump(event, temporary_file, indent=2)
        temporary_file.write("\n")
    os.replace(temporary_path, event_path)
    LOGGER.warning("Emitted retraining event %s", event_path)
    return str(event_path)


@task
def no_retraining_needed(result: DriftResult) -> None:
    """Record that the current production window remains below threshold."""
    LOGGER.info(
        "No retraining event: drift share %.3f is below %.3f",
        result["drift_share"],
        RETRAINING_SHARE_THRESHOLD,
    )


@dag(
    dag_id="airbnb_drift_monitoring",
    description="Monitor production drift and conditionally request retraining.",
    schedule="0 3 * * *",
    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    tags=["airbnb", "monitoring", "drift", "evidently"],
)
def monitoring_pipeline() -> None:
    """Define daily drift monitoring and conditional event publication."""
    result = execute_drift_check()
    branch = drift_threshold_gate(result)  # type: ignore[arg-type]
    emitted = emit_retraining_event(result)  # type: ignore[arg-type]
    unchanged = no_retraining_needed(result)  # type: ignore[arg-type]
    branch >> [emitted, unchanged]


monitoring_dag: DAG = monitoring_pipeline()
