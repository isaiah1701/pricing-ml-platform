"""Tests for exact code, data, model, and attempt provenance."""

from __future__ import annotations

import logging
import subprocess
import tarfile
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import pytest
from mlflow import MlflowClient
from sklearn.dummy import DummyRegressor

from serving.app.config import Settings
from serving.app.model_loader import load_model_version
from training.lineage import publish_dataset_snapshot
from training.provenance import build_code_snapshot, sha256_directory
from training.registry import tag_version_before_alias
from training.train import (
    create_training_attempt,
    log_model_run,
    update_training_attempt,
)

LOGGER = logging.getLogger(__name__)


def _git(repository: Path, *arguments: str) -> None:
    """Run a successful Git command in a temporary test repository."""
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_code_snapshot_captures_dirty_and_untracked_source(tmp_path: Path) -> None:
    """Change the code identity when tracked or untracked source changes."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    source = repository / "train.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")

    clean = build_code_snapshot(repository, tmp_path / "archives")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    (repository / "new_source.py").write_text("ADDED = True\n", encoding="utf-8")
    dirty = build_code_snapshot(repository, tmp_path / "archives")

    assert clean["git_dirty"] is False
    assert dirty["git_dirty"] is True
    assert clean["git_commit"] == dirty["git_commit"]
    assert clean["code_sha256"] != dirty["code_sha256"]
    with tarfile.open(dirty["archive_path"], "r:gz") as archive:
        assert "new_source.py" in archive.getnames()


def test_directory_hash_changes_with_model_artifact_bytes(tmp_path: Path) -> None:
    """Version a model artifact directory from relative names and file bytes."""
    artifact_directory = tmp_path / "model"
    artifact_directory.mkdir()
    model_file = artifact_directory / "model.pkl"
    model_file.write_bytes(b"first")
    first = sha256_directory(artifact_directory)
    model_file.write_bytes(b"second")
    assert sha256_directory(artifact_directory) != first


def test_code_snapshot_works_without_git_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash deployed source files when a production image omits the Git directory."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("GIT_COMMIT", "deployed-commit")
    deployed_root = tmp_path / "deployed"
    deployed_root.mkdir()
    (deployed_root / "train.py").write_text("VALUE = 1\n", encoding="utf-8")
    provenance = build_code_snapshot(deployed_root, tmp_path / "archives")
    assert provenance["git_state_available"] is False
    assert provenance["git_commit"] == "deployed-commit"
    assert provenance["file_count"] == 1
    assert len(provenance["code_sha256"]) == 64


def test_failed_attempt_is_retained_in_mlflow(tmp_path: Path) -> None:
    """Terminate an attempt as failed even when no model artifact was produced."""
    tracking_uri = f"sqlite:///{tmp_path / 'failed-attempt.db'}"
    experiment_name = "failed-attempt"
    MlflowClient(tracking_uri=tracking_uri).create_experiment(
        experiment_name,
        artifact_location=(tmp_path / "failed-artifacts").resolve().as_uri(),
    )
    attempt_id = create_training_attempt(
        tracking_uri, experiment_name, "test_failure"
    )
    update_training_attempt(
        tracking_uri,
        attempt_id,
        "validation_failed",
        outcome="failed",
        error=ValueError("invalid schema"),
    )

    attempt = MlflowClient(tracking_uri=tracking_uri).get_run(attempt_id)
    assert attempt.info.status == "FAILED"
    assert attempt.data.tags["attempt_stage"] == "validation_failed"
    assert attempt.data.tags["failure_type"] == "ValueError"


def test_mlflow_run_binds_code_data_model_and_parent_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist one release bundle and all reproducibility artifacts in MLflow."""
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    experiment_name = "provenance-integration"
    MlflowClient(tracking_uri=tracking_uri).create_experiment(
        experiment_name,
        artifact_location=(tmp_path / "artifacts").resolve().as_uri(),
    )
    attempt_id = create_training_attempt(
        tracking_uri, experiment_name, "test_training"
    )
    raw_path = tmp_path / "raw.csv"
    raw_path.write_text("id,value\n1,1\n", encoding="utf-8")
    dataset_path = tmp_path / "features.parquet"
    pd.DataFrame({"id": [1, 2], "value": [1.0, 2.0]}).to_parquet(
        dataset_path, index=False
    )
    published = publish_dataset_snapshot(
        dataset_path,
        tmp_path / "snapshots",
        source_path=raw_path,
        validation_report={"valid": True, "blocking_failures": []},
        transformation_name="tests.transform",
        git_commit="test",
        feature_service_name="test_features_v1",
    )
    x_train = pd.DataFrame({"value": [1.0, 2.0]})
    x_test = pd.DataFrame({"value": [3.0]})
    model = DummyRegressor(strategy="mean").fit(x_train, np.array([10.0, 20.0]))
    monkeypatch.setattr(
        "training.train.CODE_SNAPSHOT_DIRECTORY", tmp_path / "code-snapshots"
    )
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)

    report = log_model_run(
        model,
        x_train,
        x_test,
        {"mae": 0.0},
        run_name="provenance_candidate",
        model_family="dummy",
        dataset_version=published["dataset_sha256"],
        git_sha="test",
        feature_service_name="test_features_v1",
        parameters={"strategy": "mean"},
        evaluation_report={"valid": True},
        lineage_manifest_path=published["lineage_manifest_path"],
        training_attempt_id=attempt_id,
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(str(report["run_id"]))
    attempt = client.get_run(attempt_id)
    assert run.data.tags["mlflow.parentRunId"] == attempt_id
    assert run.data.tags["release_bundle_id"] == report["release_bundle_id"]
    assert attempt.data.tags["release_bundle_id"] == report["release_bundle_id"]
    assert len(str(report["code_sha256"])) == 64
    assert len(str(report["model_artifact_sha256"])) == 64
    artifact_names = {
        artifact.path
        for artifact in client.list_artifacts(str(report["run_id"]))
    }
    assert {"code", "data", "lineage", "provenance"}.issubset(artifact_names)

    registered = mlflow.register_model(str(report["model_uri"]), "test-price-model")
    tag_version_before_alias(
        client,
        "test-price-model",
        str(registered.version),
        "champion",
        {
            "release_bundle_id": str(report["release_bundle_id"]),
            "training_attempt_id": attempt_id,
            "mlflow_run_id": str(report["run_id"]),
            "code_sha256": str(report["code_sha256"]),
            "dataset_sha256": published["dataset_sha256"],
            "model_artifact_sha256": str(report["model_artifact_sha256"]),
        },
    )
    registered_metadata = client.get_model_version(
        "test-price-model", str(registered.version)
    )
    assert registered_metadata.run_id == report["run_id"]
    loaded = load_model_version(
        Settings(
            MLFLOW_TRACKING_URI=tracking_uri,
            MODEL_NAME="test-price-model",
            MODEL_ALIAS="champion",
        ),
        str(registered.version),
    )
    assert loaded.predict(x_test).tolist() == [15.0]
