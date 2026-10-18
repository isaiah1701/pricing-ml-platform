"""Explicit model deployment using existing evaluation, approval and recovery boundaries."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from airflow.sdk import DAG, dag, get_current_context, task
from mlflow import MlflowClient

from training.deployment import deploy_approved_version

LOGGER = logging.getLogger(__name__)


@task(retries=0)
def select_approved_model() -> str:
    """Verify the selected approved registry version without training or rebuilding images."""
    context: dict[str, Any] = get_current_context()
    configuration = context["dag_run"].conf or {}
    version = configuration.get("model_version", "")
    if not isinstance(version, str):
        raise TypeError("model_version must be a registry version string")
    client = MlflowClient(tracking_uri=os.environ["MLFLOW_TRACKING_URI"])
    return deploy_approved_version(
        client, os.getenv("MODEL_NAME", "airbnb-price-predictor"), version
    )


@dag(
    dag_id="airbnb_model_deploy",
    schedule=None,
    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    tags=["airbnb", "deployment", "approval"],
)
def deployment_pipeline() -> None:
    """Expose a manual model-selection entry point that reuses approved registry artifacts."""
    select_approved_model()


deployment_dag: DAG = deployment_pipeline()
