"""Reject unresolved production coordinates and optionally verify ECR images."""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORDINATE_PATHS = (
    "ingress.host",
    "platform.awsRegion",
    "platform.mlflow.databaseSecretArn",
    "platform.mlflow.databaseHost",
    "platform.mlflow.databasePort",
    "platform.mlflow.databaseName",
    "platform.mlflow.artifactRoot",
    "platform.airflow.databaseSecretArn",
    "platform.airflow.databaseHost",
    "platform.airflow.databasePort",
    "platform.airflow.databaseName",
    "platform.airflow.dataBucket",
    "platform.airflow.redisConnectionString",
)


def _value(values: dict[str, Any], path: str) -> str:
    """Read one scalar Helm value by its dotted path."""
    current: Any = values
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return ""
        current = current[key]
    return "" if current is None else str(current)


def validate_values(values: dict[str, Any], *, check_images: bool = False) -> None:
    """Fail before promotion when coordinates or immutable images are missing."""
    LOGGER.info("Validating production coordinates and immutable image references")
    invalid = [
        path
        for path in COORDINATE_PATHS
        if not _value(values, path).strip() or "REPLACE_WITH" in _value(values, path)
    ]
    for image in ("image", "trainingImage"):
        repository = _value(values, f"{image}.repository")
        tag = _value(values, f"{image}.tag")
        if not re.fullmatch(
            r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9_./-]+", repository
        ):
            invalid.append(f"{image}.repository")
        if not re.fullmatch(r"sha-[0-9a-f]{40}", tag):
            invalid.append(f"{image}.tag")
    redis = _value(values, "platform.airflow.redisConnectionString")
    if "://" in redis or not redis.endswith(",ssl=true"):
        invalid.append("platform.airflow.redisConnectionString (Feast TLS format)")
    if invalid:
        LOGGER.error("Production deployment is incomplete: %s", ", ".join(invalid))
        raise ValueError(
            "Unresolved or invalid production values: " + ", ".join(invalid)
        )
    if check_images:
        for image in ("image", "trainingImage"):
            registry, repository = _value(values, f"{image}.repository").split("/", 1)
            subprocess.run(
                [
                    "aws",
                    "ecr",
                    "describe-images",
                    "--registry-id",
                    registry.split(".")[0],
                    "--region",
                    registry.split(".")[3],
                    "--repository-name",
                    repository,
                    "--image-ids",
                    f"imageTag={_value(values, f'{image}.tag')}",
                    "--query",
                    "imageDetails[0].imageDigest",
                    "--output",
                    "text",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        LOGGER.info("Both immutable image tags exist in ECR")
    LOGGER.info("Production deployment values passed validation")


def main() -> None:
    """Check the production values file before GitOps promotion or launch."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--values",
        type=Path,
        default=PROJECT_ROOT / "serving/helm/values/production.yaml",
    )
    parser.add_argument("--check-images", action="store_true")
    args = parser.parse_args()
    decoded = yaml.safe_load(args.values.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        LOGGER.error("Production values must be a YAML mapping")
        raise TypeError("Production values must be a YAML mapping")
    validate_values(decoded, check_images=args.check_images)


if __name__ == "__main__":
    main()
