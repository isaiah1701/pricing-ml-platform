"""Check the reviewed serving image against the actual Rollout and running pod digests."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

import yaml

LOGGER = logging.getLogger(__name__)
VALUES = Path(__file__).resolve().parents[1] / "serving/helm/values/production.yaml"


def verify_image() -> None:
    """Require the intended GitOps image reference and immutable ECR digest on ready desired pods."""
    image = yaml.safe_load(VALUES.read_text())["image"]
    repository = image["repository"]
    if not re.fullmatch(
        r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9_./-]+", repository
    ):
        raise ValueError("Production image must reference its intended ECR repository")
    registry, repository_name = repository.split("/", 1)
    digest = (
        image.get("digest")
        or subprocess.check_output(
            [
                "aws",
                "ecr",
                "describe-images",
                "--region",
                registry.split(".")[3],
                "--registry-id",
                registry.split(".")[0],
                "--repository-name",
                repository_name,
                "--image-ids",
                f"imageTag={image['tag']}",
                "--query",
                "imageDetails[0].imageDigest",
                "--output",
                "text",
            ],
            text=True,
        ).strip()
    )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Expected image digest could not be resolved")
    expected = (
        f"{repository}@{digest}"
        if image.get("digest")
        else f"{repository}:{image['tag']}"
    )
    rollout = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "-n",
                "airbnb-inference",
                "get",
                "rollout",
                "airbnb-inference",
                "-o",
                "json",
            ],
            text=True,
        )
    )
    serving = next(
        container
        for container in rollout["spec"]["template"]["spec"]["containers"]
        if container["name"] == "inference"
    )
    if serving["image"] != expected:
        raise ValueError("Live Rollout image differs from the reviewed GitOps values")
    pod_hash = rollout.get("status", {}).get("currentPodHash")
    if not pod_hash:
        raise ValueError("Rollout has no desired pod revision")
    pods = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "-n",
                "airbnb-inference",
                "get",
                "pods",
                "-l",
                f"app.kubernetes.io/instance=airbnb-inference,rollouts-pod-template-hash={pod_hash}",
                "-o",
                "json",
            ],
            text=True,
        )
    )["items"]
    active = [
        pod for pod in pods if not pod.get("metadata", {}).get("deletionTimestamp")
    ]
    if not active:
        raise ValueError("No active serving pods exist at the desired revision")
    for pod in active:
        status = next(
            (
                item
                for item in pod.get("status", {}).get("containerStatuses", [])
                if item["name"] == "inference"
            ),
            {},
        )
        if not status.get("ready") or not status.get("imageID", "").endswith(digest):
            raise ValueError("Serving pod is not ready at the reviewed image digest")
    LOGGER.info(
        "Verified reviewed image digest %s on %d serving pod(s)", digest, len(active)
    )
