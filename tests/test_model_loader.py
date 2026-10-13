"""Unit tests for registry-only model loading and configuration."""

from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from serving.app.config import Settings
from serving.app.model_loader import (
    LoadedModel,
    ModelLoadError,
    build_model_uri,
    load_champion_model,
    refresh_champion_model,
)

LOGGER = logging.getLogger(__name__)


def _governed_version(
    version: str = "7", run_id: str = "run-7"
) -> tuple[SimpleNamespace, dict[str, object]]:
    """Return consistent Registry metadata and its release-bundle artifact."""
    bundle_body: dict[str, object] = {
        "training_attempt_id": "attempt-7",
        "mlflow_run_id": run_id,
        "model_uri": "models:/logged-model",
        "model_artifact_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "dataset_lineage_id": "lineage-7",
        "code_sha256": "c" * 64,
        "git_commit": "commit-7",
        "git_dirty": False,
        "git_state_available": True,
        "feature_service_name": "airbnb_price_model_v1",
    }
    canonical = json.dumps(
        bundle_body, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    bundle_id = hashlib.sha256(canonical).hexdigest()
    tags = {
        "release_bundle_id": bundle_id,
        "training_attempt_id": "attempt-7",
        "mlflow_run_id": run_id,
        "code_sha256": "c" * 64,
        "dataset_sha256": "b" * 64,
        "model_artifact_sha256": "a" * 64,
    }
    return (
        SimpleNamespace(version=version, run_id=run_id, tags=tags),
        {"release_bundle_id": bundle_id, **bundle_body},
    )


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use documented local defaults when environment variables are absent."""
    for name in (
        "MLFLOW_TRACKING_URI",
        "MODEL_NAME",
        "MODEL_ALIAS",
        "MODEL_REFRESH_INTERVAL_SECONDS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.mlflow_tracking_uri == "http://localhost:5000"
    assert settings.model_name == "airbnb-price-predictor"
    assert settings.model_alias == "champion"
    assert settings.model_refresh_interval_seconds == 60.0
    assert settings.log_level == "INFO"


def test_model_uri_uses_configured_registry_alias() -> None:
    """Construct an alias URI without an experiment run identifier."""
    settings = Settings(MODEL_NAME="custom-model", MODEL_ALIAS="candidate")
    assert build_model_uri(settings) == "models:/custom-model@candidate"


def test_champion_loader_uses_registry_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the alias and retain the concrete loaded Registry version."""
    captured: dict[str, str] = {}
    sentinel = object()
    governed_version, release_bundle = _governed_version()

    def fake_set_tracking_uri(uri: str) -> None:
        """Capture the configured tracking URI."""
        captured["tracking_uri"] = uri

    def fake_load_model(uri: str) -> object:
        """Capture the registry URI passed to MLflow."""
        captured["model_uri"] = uri
        return sentinel

    def fake_get_version(model_name: str, alias: str) -> SimpleNamespace:
        """Return a concrete version for the configured alias."""
        captured["model_name"] = model_name
        captured["alias"] = alias
        return SimpleNamespace(version="7")

    monkeypatch.setattr(
        "serving.app.model_loader.mlflow.set_tracking_uri", fake_set_tracking_uri
    )
    monkeypatch.setattr(
        "serving.app.model_loader.mlflow.sklearn.load_model", fake_load_model
    )
    monkeypatch.setattr(
        "serving.app.model_loader.MlflowClient.get_model_version_by_alias",
        lambda self, model_name, alias: fake_get_version(model_name, alias),
    )
    monkeypatch.setattr(
        "serving.app.model_loader.MlflowClient.get_model_version",
        lambda self, model_name, version: governed_version,
    )
    monkeypatch.setattr(
        "serving.app.model_loader.mlflow.artifacts.load_dict",
        lambda artifact_uri: release_bundle,
    )
    settings = Settings(
        MLFLOW_TRACKING_URI="http://mlflow.test:5000",
        MODEL_NAME="custom-model",
        MODEL_ALIAS="champion",
    )
    loaded = load_champion_model(settings)
    assert loaded.model is sentinel
    assert loaded.version == "7"
    assert loaded.model_uri == "models:/custom-model/7"
    assert captured == {
        "tracking_uri": "http://mlflow.test:5000",
        "model_name": "custom-model",
        "alias": "champion",
        "model_uri": "models:/custom-model/7",
    }


def test_champion_loader_rejects_incomplete_registry_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to load an alias target without its governed release bundle."""
    monkeypatch.setattr(
        "serving.app.model_loader.MlflowClient.get_model_version_by_alias",
        lambda self, model_name, alias: SimpleNamespace(version="7"),
    )
    monkeypatch.setattr(
        "serving.app.model_loader.MlflowClient.get_model_version",
        lambda self, model_name, version: SimpleNamespace(
            version="7", run_id="run-7", tags={}
        ),
    )
    settings = Settings(MODEL_NAME="prices", MODEL_ALIAS="champion")
    with pytest.raises(ModelLoadError, match="incomplete provenance"):
        load_champion_model(settings)


def test_champion_loader_rejects_tampered_release_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse a Registry version whose bundle content changed after creation."""
    governed_version, release_bundle = _governed_version()
    release_bundle["dataset_sha256"] = "d" * 64
    monkeypatch.setattr(
        "serving.app.model_loader.MlflowClient.get_model_version_by_alias",
        lambda self, model_name, alias: SimpleNamespace(version="7"),
    )
    monkeypatch.setattr(
        "serving.app.model_loader.MlflowClient.get_model_version",
        lambda self, model_name, version: governed_version,
    )
    monkeypatch.setattr(
        "serving.app.model_loader.mlflow.artifacts.load_dict",
        lambda artifact_uri: release_bundle,
    )
    settings = Settings(MODEL_NAME="prices", MODEL_ALIAS="champion")
    with pytest.raises(ModelLoadError, match="integrity check"):
        load_champion_model(settings)


class FixedModel:
    """Minimal prediction model used to distinguish refreshed artifacts."""

    def __init__(self, value: float) -> None:
        """Store the deterministic prediction value."""
        self.value = value

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Return one fixed value for every feature row."""
        return np.full(len(features), self.value)


def test_refresh_replaces_model_after_successful_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a fully loaded replacement when the alias version changes."""
    settings = Settings(MODEL_NAME="prices", MODEL_ALIAS="champion")
    current = LoadedModel(FixedModel(1.0), "1", "models:/prices/1")
    replacement = LoadedModel(FixedModel(2.0), "2", "models:/prices/2")

    def resolve_replacement(value: Settings) -> str:
        """Resolve the test alias to a changed version."""
        del value
        return "2"

    def load_replacement(value: Settings, version: str) -> LoadedModel:
        """Return the completely loaded replacement test model."""
        del value
        assert version == "2"
        return replacement

    monkeypatch.setattr(
        "serving.app.model_loader.resolve_alias_version", resolve_replacement
    )
    monkeypatch.setattr(
        "serving.app.model_loader.load_model_version",
        load_replacement,
    )
    refreshed = refresh_champion_model(current, settings)
    assert refreshed is replacement
    assert refreshed.version == "2"


def test_refresh_retains_current_model_when_replacement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep serving the current artifact when a changed alias cannot load."""
    settings = Settings(MODEL_NAME="prices", MODEL_ALIAS="champion")
    current = LoadedModel(FixedModel(1.0), "1", "models:/prices/1")

    def resolve_replacement(value: Settings) -> str:
        """Resolve the test alias to a changed version."""
        del value
        return "2"

    monkeypatch.setattr(
        "serving.app.model_loader.resolve_alias_version", resolve_replacement
    )

    def fail_load(value: Settings, version: str) -> LoadedModel:
        """Simulate a corrupt or unavailable replacement artifact."""
        del value, version
        raise ModelLoadError("load failed")

    monkeypatch.setattr("serving.app.model_loader.load_model_version", fail_load)
    assert refresh_champion_model(current, settings) is current
