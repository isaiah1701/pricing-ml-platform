"""Sync one approved existing Argo application at the reviewed Git revision."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
APPLICATIONS = {"airbnb-inference", "airbnb-platform", "prod-mlops-bootstrap"}


def _application(name: str) -> dict[str, Any]:
    """Read only the selected application's status and source metadata."""
    return json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "-n",
                "argocd",
                "get",
                "application",
                name,
                "-o",
                "json",
            ],
            text=True,
        )
    )


def sync(
    name: str,
    revision: str,
    repository: str,
    *,
    timeout: float = 1500,
    declared_source: dict[str, Any] | None = None,
) -> None:
    """Request an Argo sync and require successful operation, exact revision and health."""
    if declared_source is None and (
        name not in APPLICATIONS or not re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        raise ValueError("Select a known application and an immutable Git revision")
    application = _application(name)
    if application.get("operation"):
        raise RuntimeError(
            "An Argo operation is already active; inspect it before retrying"
        )
    source = application["spec"]["source"]
    if declared_source is not None:
        valid_source = (
            source == declared_source and revision == declared_source["targetRevision"]
        )
    else:
        valid_source = (
            source["repoURL"].removesuffix(".git") == f"https://github.com/{repository}"
            and source["path"] == "serving/helm"
            and source["targetRevision"] == "main"
        )
    if not valid_source:
        LOGGER.error("Argo application does not match the reviewed production source")
        raise ValueError("Unexpected Argo application source")
    request_id = uuid.uuid4().hex
    operation = {
        "operation": {
            "info": [{"name": "mlops-request-id", "value": request_id}],
            "sync": {
                "revision": revision,
                "syncOptions": [
                    "CreateNamespace=true",
                    "ServerSideApply=true",
                    "RespectIgnoreDifferences=true",
                ],
            },
        }
    }
    LOGGER.info("Syncing %s to reviewed commit %s", name, revision)
    subprocess.run(
        [
            "kubectl",
            "-n",
            "argocd",
            "patch",
            "application",
            name,
            "--type=merge",
            "-p",
            json.dumps(operation),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _application(name).get("status", {})
        operation_status = status.get("operationState", {})
        operation_revision = operation_status.get("syncResult", {}).get("revision")
        phase = operation_status.get("phase")
        info = operation_status.get("operation", {}).get("info", [])
        matching_request = any(
            item.get("name") == "mlops-request-id" and item.get("value") == request_id
            for item in info
        )
        if (
            matching_request
            and operation_revision == revision
            and phase in {"Failed", "Error"}
        ):
            LOGGER.error("Argo sync failed for %s", name)
            raise RuntimeError("Argo sync failed; inspect the application")
        if (
            matching_request
            and operation_revision == revision
            and phase == "Succeeded"
            and status.get("sync", {}).get("revision") == revision
            and status.get("sync", {}).get("status") == "Synced"
            and status.get("health", {}).get("status") == "Healthy"
        ):
            LOGGER.info("%s is synced and healthy at %s", name, revision)
            return
        time.sleep(10)
    raise TimeoutError("Argo did not become healthy at the reviewed revision")


def controller_applications() -> list[dict[str, Any]]:
    """Render the reviewed root chart and order Argo-owned controllers by their declared waves."""
    root = Path(__file__).resolve().parents[1]
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "prod-mlops-bootstrap",
            str(root / "serving/helm"),
            "--namespace",
            "argocd",
            "-f",
            str(root / "serving/helm/values/bootstrap.yaml"),
        ],
        text=True,
    )
    applications = [
        item
        for item in yaml.safe_load_all(rendered)
        if item
        and item.get("kind") == "Application"
        and item["metadata"]["name"] not in {"airbnb-inference", "airbnb-platform"}
    ]
    return sorted(
        applications,
        key=lambda item: (
            int(
                item["metadata"]
                .get("annotations", {})
                .get("argocd.argoproj.io/sync-wave", "0")
            ),
            item["metadata"]["name"],
        ),
    )


def main() -> None:
    """Sync selected applications through existing Argo ownership, never direct Helm releases."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    name = os.environ["ARGO_APPLICATION"]
    revision = os.environ["GITHUB_SHA"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if name == "controllers":
        sync("prod-mlops-bootstrap", revision, repository)
        for application in controller_applications():
            source = application["spec"]["source"]
            sync(
                application["metadata"]["name"],
                source["targetRevision"],
                repository,
                declared_source=source,
            )
    else:
        sync(name, revision, repository)
        if name == "airbnb-inference":
            from scripts.verify_rollout import verify_image

            verify_image()


if __name__ == "__main__":
    main()
