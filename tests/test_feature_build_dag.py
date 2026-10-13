"""Structural tests for the Airbnb feature-build Airflow DAG."""

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import pytest

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAG_PATH = PROJECT_ROOT / "pipelines" / "feature-build.py"


def _load_dag_module() -> ModuleType:
    """Load the hyphenated DAG file as a Python module for testing."""
    pytest.importorskip("airflow")
    specification = importlib.util.spec_from_file_location(
        "airbnb_feature_build_dag", DAG_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Could not load DAG module from {DAG_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_feature_build_dag_contains_ordered_tasks() -> None:
    """Build features through cleaning, publishing, and verification in order."""
    LOGGER.debug("Testing the feature-build DAG task graph")
    module = _load_dag_module()
    feature_build_dag = module.feature_build_dag

    expected_task_ids = {
        "build_feature_dataset",
        "validate_feature_dataset",
        "apply_feature_repository",
        "materialize_online_features",
        "verify_ready_features",
    }
    assert feature_build_dag.dag_id == "airbnb_feature_build"
    assert set(feature_build_dag.task_ids) == expected_task_ids
    assert feature_build_dag.task_dict[
        "validate_feature_dataset"
    ].upstream_task_ids == {"build_feature_dataset"}
    assert feature_build_dag.task_dict[
        "apply_feature_repository"
    ].upstream_task_ids == {"validate_feature_dataset"}
    assert feature_build_dag.task_dict[
        "materialize_online_features"
    ].upstream_task_ids == {"apply_feature_repository"}
    assert feature_build_dag.task_dict[
        "verify_ready_features"
    ].upstream_task_ids == {"materialize_online_features"}
