"""Structural and registration-gate tests for the model-training DAG."""

from __future__ import annotations

import importlib.util
import logging
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAG_PATH = PROJECT_ROOT / "pipelines" / "training.py"


def _load_dag_module() -> ModuleType:
    """Load the training DAG module from its file path."""
    pytest.importorskip("airflow")
    specification = importlib.util.spec_from_file_location("airbnb_training_dag", DAG_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Could not load DAG module from {DAG_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_training_dag_has_strict_lifecycle_order() -> None:
    """Require validation through conditional registration in strict order."""
    module = _load_dag_module()
    dag = module.training_dag
    ordered = [
        "validate_training_dataset",
        "retrieve_point_in_time_features",
        "train_models",
        "evaluate_models",
        "log_models_to_mlflow",
        "conditional_registration",
    ]
    assert dag.dag_id == "airbnb_model_training"
    assert set(dag.task_ids) == set(ordered)
    for upstream, downstream in pairwise(ordered):
        assert dag.task_dict[downstream].upstream_task_ids == {upstream}


def test_registration_gate_requires_mae_and_reload_verification() -> None:
    """Reject candidates that fail either explicit registration condition."""
    module = _load_dag_module()
    assert module.registration_gate(49.9, 50.0, True) is True
    assert module.registration_gate(50.0, 50.0, True) is False
    assert module.registration_gate(49.9, 50.0, False) is False
