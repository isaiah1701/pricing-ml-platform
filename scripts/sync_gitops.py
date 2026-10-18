"""Sync one approved existing Argo application at the reviewed Git revision."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any

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


def sync(name: str, revision: str, repository: str, *, timeout: float = 1500) -> None:
    """Request an Argo sync and require successful operation, exact revision and health."""
    if name not in APPLICATIONS or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Select a known application and an immutable Git revision")
    application = _application(name)
    source = application["spec"]["source"]
    if (
        source["repoURL"].removesuffix(".git") != f"https://github.com/{repository}"
        or source["path"] != "serving/helm"
        or source["targetRevision"] != "main"
    ):
        LOGGER.error(
            "Argo application does not match the reviewed production Git source"
        )
        raise ValueError("Unexpected Argo application source")
    operation = {
        "operation": {
            "sync": {
                "revision": revision,
                "syncOptions": [
                    "CreateNamespace=true",
                    "ServerSideApply=true",
                    "RespectIgnoreDifferences=true",
                ],
            }
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
        if operation_revision == revision and phase in {"Failed", "Error"}:
            LOGGER.error("Argo sync failed for %s", name)
            raise RuntimeError("Argo sync failed; inspect the application")
        if (
            operation_revision == revision
            and phase == "Succeeded"
            and status.get("sync", {}).get("revision") == revision
            and status.get("sync", {}).get("status") == "Synced"
            and status.get("health", {}).get("status") == "Healthy"
        ):
            LOGGER.info("%s is synced and healthy at %s", name, revision)
            return
        time.sleep(10)
    raise TimeoutError("Argo did not become healthy at the reviewed revision")


def main() -> None:
    """Use the explicit workflow application and GitHub source identity."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sync(
        os.environ["ARGO_APPLICATION"],
        os.environ["GITHUB_SHA"],
        os.environ["GITHUB_REPOSITORY"],
    )


if __name__ == "__main__":
    main()
