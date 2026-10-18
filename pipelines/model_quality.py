"""Scheduled Airflow DAG for delayed production model-quality monitoring."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from airflow.sdk import DAG, dag, task

from monitoring.model_quality import ModelQualityResult, run_model_quality_check
from monitoring.operational_metrics import pipeline_failure, pipeline_success

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTCOMES_PATH = Path(
    os.getenv(
        "MODEL_QUALITY_OUTCOMES_PATH",
        PROJECT_ROOT / "data" / "model_outcomes.parquet",
    )
).expanduser().resolve()
REPORT_DIR = Path(
    os.getenv("MODEL_QUALITY_REPORT_DIR", PROJECT_ROOT / "monitoring" / "reports")
).expanduser().resolve()
METRICS_PATH = Path(
    os.getenv(
        "MODEL_QUALITY_PROMETHEUS_METRICS_PATH",
        PROJECT_ROOT / "monitoring" / "metrics" / "model_quality.prom",
    )
).expanduser().resolve()
WINDOW_DAYS = int(os.getenv("MODEL_QUALITY_WINDOW_DAYS", "30"))
REFERENCE_MAE_DOLLARS = float(os.getenv("MODEL_QUALITY_REFERENCE_MAE_DOLLARS", "54.67"))
SEGMENT_COLUMNS = tuple(
    column.strip()
    for column in os.getenv(
        "MODEL_QUALITY_SEGMENT_COLUMNS", "room_type,neighbourhood_group"
    ).split(",")
    if column.strip()
)


@task(retries=1)
def calculate_production_quality() -> ModelQualityResult:
    """Calculate rolling quality from matched predictions and actual outcomes."""
    LOGGER.info("Starting production model-quality monitoring")
    return run_model_quality_check(
        OUTCOMES_PATH,
        REPORT_DIR,
        METRICS_PATH,
        window_days=WINDOW_DAYS,
        reference_mae_dollars=REFERENCE_MAE_DOLLARS,
        segment_columns=SEGMENT_COLUMNS,
    )


@dag(
    dag_id="airbnb_model_quality_monitoring",
    description="Measure delayed production quality from matched outcomes.",
    schedule="0 4 * * *",
    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    on_success_callback=pipeline_success,
    on_failure_callback=pipeline_failure,
    tags=["airbnb", "monitoring", "quality", "outcomes"],
)
def model_quality_pipeline() -> None:
    """Define daily delayed production-quality monitoring."""
    calculate_production_quality()


model_quality_dag: DAG = model_quality_pipeline()
