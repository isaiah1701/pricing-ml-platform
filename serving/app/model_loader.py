"""Load prediction models exclusively from an MLflow Registry alias."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from mlflow import MlflowClient

from serving.app.config import Settings

LOGGER = logging.getLogger(__name__)

REQUIRED_PROVENANCE_TAGS: frozenset[str] = frozenset(
    {
        "release_bundle_id",
        "training_attempt_id",
        "mlflow_run_id",
        "code_sha256",
        "dataset_sha256",
        "model_artifact_sha256",
    }
)
SHA256_TAGS: frozenset[str] = frozenset(
    {
        "release_bundle_id",
        "code_sha256",
        "dataset_sha256",
        "model_artifact_sha256",
    }
)


class PredictionModel(Protocol):
    """Describe the DataFrame prediction interface required by the API."""

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Predict one nightly price for every input row."""
        ...


class ModelLoadError(RuntimeError):
    """Indicate a failed alias resolution or load, with a known target if available."""

    def __init__(
        self, message: str, *, target_version: str | None = None,
        registry_available: bool | None = None,
    ) -> None:
        """Retain a resolved target without disclosing registry exception details."""
        super().__init__(message)
        self.target_version = target_version
        self.registry_available = registry_available


@dataclass(frozen=True)
class LoadedModel:
    """Retain a prediction model together with its resolved registry identity."""

    model: PredictionModel
    version: str
    model_uri: str

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Delegate inference to the immutable loaded estimator reference."""
        return self.model.predict(features)


def build_model_uri(settings: Settings) -> str:
    """Build the registry URI without coupling inference to a run ID."""
    return f"models:/{settings.model_name}@{settings.model_alias}"


def resolve_alias_version(settings: Settings) -> str:
    """Resolve the configured Registry alias to its concrete model version."""
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    try:
        version = MlflowClient().get_model_version_by_alias(
            settings.model_name, settings.model_alias
        )
    except Exception as exc:
        LOGGER.error("Registry alias could not be resolved")
        raise ModelLoadError(
            "Configured registry alias is unavailable", registry_available=False
        ) from exc
    return str(version.version)


def _is_sha256(value: str) -> bool:
    """Return whether a string is one lowercase or uppercase SHA-256 digest."""
    return len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _require_registry_provenance(model_version: Any) -> None:
    """Reject a Registry version without a valid code-data-model release bundle."""
    tags_value = getattr(model_version, "tags", None)
    if not isinstance(tags_value, Mapping):
        LOGGER.error("Registry model version has no provenance tags")
        raise ModelLoadError("Registry model version is missing provenance")
    tags = {str(key): str(value) for key, value in tags_value.items()}
    missing = sorted(
        key for key in REQUIRED_PROVENANCE_TAGS if not tags.get(key, "").strip()
    )
    if missing:
        LOGGER.error("Registry model version is missing provenance tags: %s", missing)
        raise ModelLoadError("Registry model version has incomplete provenance")
    invalid_hashes = sorted(key for key in SHA256_TAGS if not _is_sha256(tags[key]))
    if invalid_hashes:
        LOGGER.error(
            "Registry model version has invalid provenance hashes: %s", invalid_hashes
        )
        raise ModelLoadError("Registry model version has invalid provenance hashes")

    run_id = str(getattr(model_version, "run_id", "") or "")
    if not run_id or tags["mlflow_run_id"] != run_id:
        LOGGER.error("Registry version run identity does not match provenance tags")
        raise ModelLoadError("Registry model version has inconsistent run provenance")
    try:
        bundle = mlflow.artifacts.load_dict(
            f"runs:/{run_id}/provenance/release_bundle.json"
        )
    except Exception as exc:
        LOGGER.error("Registry release-bundle artifact could not be loaded")
        raise ModelLoadError(
            "Registry release-bundle provenance is unavailable"
        ) from exc
    if not isinstance(bundle, dict):
        raise ModelLoadError("Registry release bundle must be a JSON object")
    recorded_bundle_id = str(bundle.get("release_bundle_id", ""))
    bundle_body = {
        key: value for key, value in bundle.items() if key != "release_bundle_id"
    }
    canonical = json.dumps(
        bundle_body, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    calculated_bundle_id = hashlib.sha256(canonical).hexdigest()
    if calculated_bundle_id != recorded_bundle_id or recorded_bundle_id != tags[
        "release_bundle_id"
    ]:
        LOGGER.error("Registry release-bundle identity check failed")
        raise ModelLoadError("Registry release bundle failed its integrity check")
    expected = {
        "mlflow_run_id": run_id,
        "code_sha256": tags["code_sha256"],
        "dataset_sha256": tags["dataset_sha256"],
        "model_artifact_sha256": tags["model_artifact_sha256"],
        "training_attempt_id": tags["training_attempt_id"],
    }
    mismatched = sorted(
        key for key, value in expected.items() if str(bundle.get(key, "")) != value
    )
    if mismatched:
        LOGGER.error(
            "Registry release bundle disagrees with version tags: %s", mismatched
        )
        raise ModelLoadError("Registry release bundle does not match model version")


def load_model_version(settings: Settings, version: str) -> LoadedModel:
    """Load one concrete model version with its persisted preprocessing."""
    model_uri = f"models:/{settings.model_name}/{version}"
    LOGGER.info("Loading resolved registry model %s", model_uri)
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    try:
        model_version = MlflowClient().get_model_version(
            settings.model_name, version
        )
        _require_registry_provenance(model_version)
        model = mlflow.sklearn.load_model(model_uri)
    except ModelLoadError:
        raise
    except Exception as exc:
        LOGGER.error("Registry model could not be loaded from %s", model_uri)
        raise ModelLoadError("Configured registry model is unavailable") from exc
    LOGGER.info("Registry model version %s loaded successfully", version)
    return LoadedModel(model=model, version=version, model_uri=model_uri)


def load_champion_model(settings: Settings) -> LoadedModel:
    """Resolve and load the concrete model version behind the configured alias."""
    version = resolve_alias_version(settings)
    try:
        return load_model_version(settings, version)
    except ModelLoadError as exc:
        raise ModelLoadError(
            str(exc), target_version=version, registry_available=True
        ) from exc


def refresh_champion_model(
    current: LoadedModel,
    settings: Settings,
) -> LoadedModel:
    """Prepare a changed alias target; let the caller retain state on failure."""
    version = resolve_alias_version(settings)
    if version == current.version:
        return current
    try:
        replacement = load_model_version(settings, version)
    except ModelLoadError as exc:
        LOGGER.error("Model refresh could not prepare target version %s", version)
        raise ModelLoadError(
            str(exc), target_version=version, registry_available=True
        ) from exc
    LOGGER.info(
        "Model refresh prepared version %s to replace %s", version, current.version
    )
    return replacement
