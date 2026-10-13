"""Structural tests for scheduled production-quality monitoring."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAG_PATH = PROJECT_ROOT / "pipelines" / "model_quality.py"


def _load_dag_module() -> ModuleType:
    """Load the model-quality DAG module from its file path."""
    pytest.importorskip("airflow")
    specification = importlib.util.spec_from_file_location(
        "airbnb_model_quality_dag", DAG_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Could not load DAG module from {DAG_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_model_quality_dag_runs_daily_after_drift_monitoring() -> None:
    """Schedule one retryable delayed-quality task every day."""
    module = _load_dag_module()
    dag = module.model_quality_dag
    assert dag.dag_id == "airbnb_model_quality_monitoring"
    assert str(dag.schedule) == "0 4 * * *"
    assert set(dag.task_ids) == {"calculate_production_quality"}
    assert dag.task_dict["calculate_production_quality"].retries == 1
