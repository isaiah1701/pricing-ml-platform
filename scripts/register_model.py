"""Register a run's logged model and assign the champion registry alias."""

from __future__ import annotations

import logging
import os
import re

import mlflow
from mlflow import MlflowClient
from mlflow.entities import LoggedModel, Run
from mlflow.entities.model_registry import ModelVersion

LOGGER = logging.getLogger(__name__)

DEFAULT_TRACKING_URI = "http://localhost:5000"
DEFAULT_REGISTERED_MODEL_NAME = "airbnb-price-predictor"
MODEL_ARTIFACT_NAME = "model"
DEFAULT_MODEL_ALIAS = "champion"
RUN_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
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


def get_required_run_id() -> str:
    """Read and validate the champion run ID from the environment."""
    run_id = os.getenv("CHAMPION_RUN_ID", "").strip()
    if not run_id:
        LOGGER.error("CHAMPION_RUN_ID is not set")
        raise ValueError(
            "CHAMPION_RUN_ID is required; set it to the selected MLflow run ID"
        )
    if not RUN_ID_PATTERN.fullmatch(run_id):
        LOGGER.error("CHAMPION_RUN_ID is not a valid MLflow run ID")
        raise ValueError("CHAMPION_RUN_ID must be a 32-character hexadecimal run ID")
    return run_id


def find_logged_model(run: Run) -> LoggedModel:
    """Resolve the ready model artifact logged by the selected MLflow 3 run."""
    logged_models = mlflow.search_logged_models(
        experiment_ids=[run.info.experiment_id],
        filter_string=f"source_run_id = '{run.info.run_id}'",
        output_format="list",
    )
    candidates = [
        model
        for model in logged_models
        if model.name == MODEL_ARTIFACT_NAME and model.status.value == "READY"
    ]
    if not candidates:
        LOGGER.error("Run %s has no ready '%s' model", run.info.run_id, MODEL_ARTIFACT_NAME)
        raise ValueError(
            f"Run {run.info.run_id} has no ready model named {MODEL_ARTIFACT_NAME!r}"
        )
    selected = max(candidates, key=lambda model: model.creation_timestamp)
    LOGGER.info("Resolved logged model %s from run %s", selected.model_id, run.info.run_id)
    return selected


def find_existing_version(
    client: MlflowClient,
    model_name: str,
    run_id: str,
    logged_model_id: str,
) -> ModelVersion | None:
    """Return an existing version for the same source to keep registration idempotent."""
    versions = client.search_model_versions(f"name='{model_name}'")
    matching = [
        version
        for version in versions
        if version.tags.get("source_run_id") == run_id
        and version.tags.get("source_logged_model_id") == logged_model_id
    ]
    if not matching:
        return None
    return max(matching, key=lambda version: int(version.version))


def set_version_metadata(
    client: MlflowClient,
    model_name: str,
    version: ModelVersion,
    run: Run,
    logged_model: LoggedModel,
) -> None:
    """Attach source, dataset, and evaluation metadata to a model version."""
    candidate_metadata: dict[str, object | None] = {
        "source_run_id": run.info.run_id,
        "mlflow_run_id": run.info.run_id,
        "source_logged_model_id": logged_model.model_id,
        "source_artifact_name": logged_model.name,
        "dataset_version": run.data.params.get("dataset_version"),
        "dataset_sha256": run.data.params.get("dataset_sha256"),
        "feature_service_name": run.data.params.get("feature_service_name"),
        "mae": run.data.metrics.get("mae"),
        "rmse": run.data.metrics.get("rmse"),
        "r2": run.data.metrics.get("r2"),
        "ordinary_market_mae": run.data.metrics.get("mae_ordinary_market"),
        "ordinary_market_r2": run.data.metrics.get("r2_ordinary_market"),
        "ordinary_market_coverage": run.data.metrics.get(
            "ordinary_market_coverage"
        ),
        "model_family": run.data.tags.get("model_family"),
        "git_sha": run.data.tags.get("mlflow.source.git.commit"),
        "release_bundle_id": run.data.tags.get("release_bundle_id"),
        "training_attempt_id": run.data.tags.get("training_attempt_id"),
        "code_sha256": run.data.tags.get("code_sha256"),
        "model_artifact_sha256": run.data.tags.get("model_artifact_sha256"),
        "registered_by": "scripts/register_model.py",
    }
    metadata = {
        key: str(value)
        for key, value in candidate_metadata.items()
        if value is not None and str(value).strip()
    }
    missing_provenance = sorted(REQUIRED_PROVENANCE_TAGS.difference(metadata))
    if missing_provenance:
        LOGGER.error(
            "Selected run is missing required provenance: %s", missing_provenance
        )
        raise ValueError(
            f"Selected run is missing required provenance: {missing_provenance}"
        )
    for key, value in metadata.items():
        client.set_model_version_tag(
            name=model_name,
            version=version.version,
            key=key,
            value=value,
        )
    client.update_model_version(
        name=model_name,
        version=version.version,
        description=(
            "Champion NYC Airbnb nightly-price ensemble. The source run, dataset "
            "version, feature service, and held-out metrics are recorded as tags."
        ),
    )
    client.set_registered_model_tag(model_name, "domain", "airbnb")
    client.set_registered_model_tag(model_name, "task", "price_regression")


def register_champion() -> ModelVersion:
    """Register the selected run model and atomically move the champion alias."""
    run_id = get_required_run_id()
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", DEFAULT_TRACKING_URI).strip()
    model_name = os.getenv("MODEL_NAME", DEFAULT_REGISTERED_MODEL_NAME).strip()
    model_alias = os.getenv("MODEL_ALIAS", DEFAULT_MODEL_ALIAS).strip()
    if not tracking_uri:
        LOGGER.error("MLFLOW_TRACKING_URI cannot be empty")
        raise ValueError("MLFLOW_TRACKING_URI cannot be empty")
    if not model_name or not model_alias:
        LOGGER.error("MODEL_NAME and MODEL_ALIAS cannot be empty")
        raise ValueError("MODEL_NAME and MODEL_ALIAS cannot be empty")
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    LOGGER.info("Registering champion from run %s via %s", run_id, tracking_uri)
    run = client.get_run(run_id)
    if run.info.status != "FINISHED":
        LOGGER.error("Selected run %s is not finished", run_id)
        raise ValueError(f"Run {run_id} must be FINISHED before registration")
    logged_model = find_logged_model(run)
    version = find_existing_version(client, model_name, run_id, logged_model.model_id)
    if version is None:
        version = mlflow.register_model(
            model_uri=logged_model.model_uri,
            name=model_name,
            await_registration_for=300,
        )
        LOGGER.info("Created registered model version %s", version.version)
    else:
        LOGGER.info("Reusing registered model version %s", version.version)
    set_version_metadata(client, model_name, version, run, logged_model)
    client.set_registered_model_alias(
        name=model_name,
        alias=model_alias,
        version=version.version,
    )
    return client.get_model_version(model_name, version.version)


def main() -> None:
    """Configure logging, register the model, and print the resulting alias."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        version = register_champion()
    except Exception as exc:
        LOGGER.error("Model registration failed: %s", exc)
        raise SystemExit(1) from exc
    model_name = os.getenv("MODEL_NAME", DEFAULT_REGISTERED_MODEL_NAME).strip()
    model_alias = os.getenv("MODEL_ALIAS", DEFAULT_MODEL_ALIAS).strip()
    print("Registered:")
    print(model_name)
    print(f"version: {version.version}")
    print(f"alias: {model_alias}")
    print(f"source run: {version.tags['source_run_id']}")


if __name__ == "__main__":
    main()
