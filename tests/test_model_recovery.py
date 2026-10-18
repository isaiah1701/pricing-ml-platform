"""Recover a governed approved model into an empty local MLflow database."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import mlflow
import mlflow.sklearn
import pandas as pd
import pytest
from mlflow import MlflowClient
from sklearn.dummy import DummyRegressor

from serving.app.config import Settings
from serving.app.model_loader import load_champion_model
from training.provenance import sha256_directory
from training.recovery import (
    _read,
    _write,
    export_approved,
    restore_approved,
    verify_backup,
)
from training.registry import tag_version_before_alias

LOGGER = logging.getLogger(__name__)
NAME = "airbnb-price-predictor"


@pytest.fixture(scope="module")
def approved_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Export a tiny evaluated model with real serving-compatible provenance."""
    root = tmp_path_factory.mktemp("approved-model")
    uri = f"sqlite:///{root / 'source.db'}"
    mlflow.set_tracking_uri(uri)
    client = MlflowClient(tracking_uri=uri)
    experiment = client.create_experiment(
        "source", artifact_location=(root / "artifacts").as_uri()
    )
    frame = pd.DataFrame({"value": [1.0, 2.0]})
    estimator = DummyRegressor(strategy="mean").fit(frame, [10.0, 20.0])
    with mlflow.start_run(experiment_id=experiment) as run:
        model = mlflow.sklearn.log_model(
            estimator, name="model", pip_requirements=["scikit-learn"]
        )
        downloaded = mlflow.artifacts.download_artifacts(model.model_uri)
        body = {
            "mlflow_run_id": run.info.run_id,
            "training_attempt_id": "original-attempt",
            "code_sha256": "a" * 64,
            "dataset_sha256": "b" * 64,
            "model_artifact_sha256": sha256_directory(downloaded),
            "model_uri": model.model_uri,
            "git_commit": "original-commit",
        }
        identity = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        mlflow.log_dict(
            {"release_bundle_id": identity, **body}, "provenance/release_bundle.json"
        )
        mlflow.set_tag("artifact_reload_verified", "true")
        mlflow.log_metric("mae", 1.0)
        mlflow.log_param("feature_service_name", "test-v1")
    version = mlflow.register_model(model.model_uri, NAME)
    tags = {key: str(value) for key, value in body.items() if key != "model_uri"}
    tags["release_bundle_id"] = identity
    tag_version_before_alias(client, NAME, str(version.version), "champion", tags)
    store = root / "store"
    first = export_approved(client, str(store), NAME)
    assert export_approved(client, str(store), NAME) == first
    return store


def test_restore_into_fresh_registry_and_serve_without_training(
    tmp_path: Path,
    approved_store: Path,
) -> None:
    """Recover original bytes and provenance, load predictions, and repeat without duplicates."""
    uri = f"sqlite:///{tmp_path / 'empty.db'}"
    client = MlflowClient(tracking_uri=uri)
    client.create_experiment(
        "approved-model-recovery", artifact_location=(tmp_path / "recovered").as_uri()
    )
    version = restore_approved(client, str(approved_store), NAME)
    assert restore_approved(client, str(approved_store), NAME) == version
    assert len(client.search_model_versions(f"name = '{NAME}'")) == 1
    metadata = client.get_model_version(NAME, version)
    assert metadata.tags["original_version"] == "1"
    assert metadata.tags["original_run_id"] != metadata.run_id
    assert client.get_run(metadata.run_id).data.metrics["mae"] == 1.0
    settings = Settings(
        MLFLOW_TRACKING_URI=uri, MODEL_NAME=NAME, MODEL_ALIAS="champion"
    )
    loaded = load_champion_model(settings)
    assert loaded.predict(pd.DataFrame({"value": [3.0]})).tolist() == [15.0]
    # An existing champion wins even if the backup is temporarily unavailable.
    assert restore_approved(client, str(tmp_path / "missing"), NAME) == version


@pytest.mark.parametrize("corruption", ["model", "manifest", "pointer", "path"])
def test_corruption_is_rejected_before_registry_mutation(
    tmp_path: Path,
    approved_store: Path,
    corruption: str,
) -> None:
    """Fail closed on corrupt metadata, model bytes and unsafe paths."""
    import shutil

    store = tmp_path / "store"
    shutil.copytree(approved_store, store)
    pointer_path = store / f"aliases/{NAME}/champion.json"
    pointer = json.loads(pointer_path.read_text())
    directory = store / "bundles" / pointer["bundle_sha256"]
    manifest_path = directory / "manifest.json"
    if corruption == "model":
        manifest = json.loads(manifest_path.read_text())
        model_file = next(
            path for path in manifest["files"] if path.endswith((".pkl", ".skops"))
        )
        (directory / "model" / model_file).write_bytes(b"corrupt")
    elif corruption == "manifest":
        manifest_path.write_text("{}")
    elif corruption == "pointer":
        pointer_path.write_text('{"bundle_sha256":"../outside"}')
    else:
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["../outside"] = "a" * 64
        data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(data).hexdigest()
        directory.rename(store / "bundles" / digest)
        (store / "bundles" / digest / "manifest.json").write_bytes(data)
        pointer_path.write_text(json.dumps({"bundle_sha256": digest}))
    client = MlflowClient(tracking_uri=f"sqlite:///{tmp_path / 'empty.db'}")
    with pytest.raises(ValueError):
        restore_approved(client, str(store), NAME)
    assert client.search_model_versions() == []
    assert client.get_experiment_by_name("approved-model-recovery") is None


def test_registry_outage_does_not_trigger_restore(tmp_path: Path) -> None:
    """Treat unavailable registry services differently from missing aliases."""
    client = Mock(spec=MlflowClient)
    client.get_registered_model.side_effect = mlflow.exceptions.MlflowException(
        "offline"
    )
    with pytest.raises(mlflow.exceptions.MlflowException):
        restore_approved(client, str(tmp_path), NAME)
    client.create_run.assert_not_called()


def test_interrupted_restore_reuses_prepared_version(
    tmp_path: Path,
    approved_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry an interrupted alias publication without duplicating registry versions."""
    client = MlflowClient(tracking_uri=f"sqlite:///{tmp_path / 'retry.db'}")
    client.create_experiment(
        "approved-model-recovery", artifact_location=(tmp_path / "artifacts").as_uri()
    )
    publish = client.set_registered_model_alias
    monkeypatch.setattr(
        client,
        "set_registered_model_alias",
        Mock(side_effect=RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        restore_approved(client, str(approved_store), NAME)
    assert len(client.search_model_versions()) == 1
    monkeypatch.setattr(client, "set_registered_model_alias", publish)
    restore_approved(client, str(approved_store), NAME)
    assert len(client.search_model_versions()) == 1


def test_s3_store_uses_existing_prefix_and_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise S3 upload/read paths without AWS credentials or network calls."""
    from botocore.exceptions import ClientError

    client = Mock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )
    constructor = Mock(return_value=client)
    monkeypatch.setattr("training.recovery.boto3.client", constructor)
    _write("s3://existing/snapshots/approved-models", "manifest.json", b"{}")
    client.put_object.assert_called_once_with(
        Bucket="existing",
        Key="snapshots/approved-models/manifest.json",
        Body=b"{}",
        ServerSideEncryption="AES256",
    )
    body: Any = Mock()
    body.read.return_value = b"{}"
    client.get_object.side_effect = None
    client.get_object.return_value = {"Body": body}
    assert _read("s3://existing/snapshots/approved-models", "manifest.json") == b"{}"


def test_retained_bundle_can_be_verified_without_a_registry(
    approved_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify S3/local backup bytes independently of the original MLflow database."""
    client = Mock(side_effect=AssertionError("verification must not open a registry"))
    monkeypatch.setattr("training.recovery.MlflowClient", client)
    identity = verify_backup(str(approved_store), NAME)
    assert len(identity) == 64
    client.assert_not_called()
