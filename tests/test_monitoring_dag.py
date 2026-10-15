"""Structural tests for scheduled and thresholded drift monitoring."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAG_PATH = PROJECT_ROOT / "pipelines" / "monitoring.py"


def _load_dag_module() -> ModuleType:
    """Load the monitoring DAG module from its file path."""
    pytest.importorskip("airflow")
    specification = importlib.util.spec_from_file_location("airbnb_monitoring_dag", DAG_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Could not load DAG module from {DAG_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_monitoring_dag_is_scheduled_and_thresholded() -> None:
    """Run drift first and place event emission behind the branch gate."""
    module = _load_dag_module()
    dag = module.monitoring_dag
    assert dag.dag_id == "airbnb_drift_monitoring"
    assert str(dag.schedule) == "0 3 * * *"
    assert set(dag.task_ids) == {
        "prepare_production_window",
        "execute_drift_check",
        "publish_drift_artifacts",
        "drift_threshold_gate",
        "emit_retraining_event",
        "no_retraining_needed",
    }
    assert dag.task_dict["execute_drift_check"].upstream_task_ids == {
        "prepare_production_window"
    }
    assert dag.task_dict["drift_threshold_gate"].upstream_task_ids == {
        "execute_drift_check",
        "publish_drift_artifacts",
    }
    assert dag.task_dict["emit_retraining_event"].upstream_task_ids == {
        "execute_drift_check",
        "drift_threshold_gate",
    }
