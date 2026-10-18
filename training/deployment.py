"""Verify and select already approved registry versions without training or image builds."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import mlflow
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from serving.app.model_loader import _require_registry_provenance
from training.provenance import sha256_directory
from training.recovery import persist_if_configured
from training.registry import tag_version_before_alias

LOGGER = logging.getLogger(__name__)


def deploy_approved_version(
    client: MlflowClient, name: str, requested: str = ""
) -> str:
    """Verify artifacts and promote only a previously approved version or the current champion."""
    if requested and (not requested.isdigit() or int(requested) < 1):
        raise ValueError("Select a positive registry version or the current champion")
    try:
        current = client.get_model_version_by_alias(name, "champion")
    except MlflowException as exc:
        LOGGER.error(
            "Champion is missing; restore the approved registry before deployment"
        )
        raise ValueError(
            "Restore champion first; deployment never starts training"
        ) from exc
    selected = client.get_model_version(name, requested or str(current.version))
    if selected.status != "READY":
        raise ValueError("Selected model artifacts are not READY")
    if (
        str(selected.version) != str(current.version)
        and selected.tags.get("promotion_status") != "approved"
    ):
        LOGGER.error("Requested version has not passed the existing approval process")
        raise ValueError(
            "Use the governed training/promotion gates before selecting this version"
        )
    run = client.get_run(selected.run_id)
    if (
        run.info.status != "FINISHED"
        or run.data.tags.get("artifact_reload_verified") != "true"
    ):
        raise ValueError(
            "Selected source run has not passed artifact reload verification"
        )
    LOGGER.info("Verifying approved model %s version %s", name, selected.version)
    # Check metadata before any model deserialization; loading happens in serving.
    mlflow.set_tracking_uri(client.tracking_uri)
    _require_registry_provenance(selected)
    with tempfile.TemporaryDirectory(prefix="approved-deploy-") as temporary:
        bundle_path = client.download_artifacts(
            selected.run_id, "provenance/release_bundle.json", temporary
        )
        bundle = json.loads(Path(bundle_path).read_text())
        artifact = mlflow.artifacts.download_artifacts(
            artifact_uri=bundle["model_uri"],
            tracking_uri=client.tracking_uri,
            dst_path=str(Path(temporary) / "model"),
        )
        if sha256_directory(artifact) != selected.tags["model_artifact_sha256"]:
            LOGGER.error(
                "Selected model artifact checksum does not match its approved provenance"
            )
            raise ValueError("Approved model artifact checksum mismatch")
    # A concurrent promotion must not silently become this operation's rollback target.
    observed = client.get_model_version_by_alias(name, "champion")
    if str(observed.version) != str(current.version):
        raise RuntimeError(
            "Champion changed during verification; inspect before retrying"
        )
    if str(selected.version) != str(current.version):
        client.set_registered_model_alias(name, "rollback", str(current.version))
        tag_version_before_alias(
            client, name, str(selected.version), "champion", selected.tags
        )
    persist_if_configured(client, name, "champion")
    LOGGER.info(
        "Approved model %s version %s is selected for deployment",
        name,
        selected.version,
    )
    return str(selected.version)
