"""Render non-secret Terraform outputs into production Helm values."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TERRAFORM_DIR = PROJECT_ROOT / "terraform" / "environments" / "prod"
DEFAULT_PRODUCTION_VALUES = PROJECT_ROOT / "serving" / "helm" / "values" / "production.yaml"
DEFAULT_BOOTSTRAP_VALUES = PROJECT_ROOT / "serving" / "helm" / "values" / "bootstrap.yaml"
REQUIRED_COORDINATES: frozenset[str] = frozenset(
    {
        "airflow_database_secret_arn",
        "application_hostname",
        "cluster_name",
        "domain_name",
        "mlflow_artifact_root",
        "mlflow_database_secret_arn",
        "platform_data_bucket",
        "redis_connection_string",
        "serving_repository",
        "training_repository",
        "aws_region",
    }
)


def load_coordinates(terraform_directory: Path) -> dict[str, str]:
    """Read and validate non-secret deployment coordinates from Terraform."""
    completed = subprocess.run(
        [
            "terraform",
            f"-chdir={terraform_directory}",
            "output",
            "-json",
            "deployment_coordinates",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    decoded = json.loads(completed.stdout)
    if not isinstance(decoded, dict):
        LOGGER.error("Terraform deployment_coordinates output was not an object")
        raise TypeError("deployment_coordinates must be a JSON object")
    coordinates = {str(key): str(value) for key, value in decoded.items()}
    missing = sorted(
        key for key in REQUIRED_COORDINATES if not coordinates.get(key, "").strip()
    )
    if missing:
        LOGGER.error("Terraform deployment coordinates are incomplete: %s", missing)
        raise ValueError(f"Missing Terraform deployment coordinates: {missing}")
    return coordinates


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load one Helm values mapping from disk."""
    decoded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        LOGGER.error("Helm values file is not a mapping: %s", path)
        raise TypeError(f"Helm values must be a mapping: {path}")
    return decoded


def _write_yaml(path: Path, values: dict[str, Any]) -> None:
    """Atomically persist one generated Helm values mapping."""
    rendered = yaml.safe_dump(values, sort_keys=False)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        temporary_file.write(rendered)
    os.replace(temporary_path, path)
    LOGGER.info("Rendered AWS deployment values to %s", path)


def render_values(
    coordinates: dict[str, str],
    production_values_path: Path,
    bootstrap_values_path: Path,
) -> None:
    """Inject Terraform outputs into workload and bootstrap Helm values."""
    production = _load_yaml(production_values_path)
    production["image"]["repository"] = coordinates["serving_repository"]
    production["trainingImage"]["repository"] = coordinates[
        "training_repository"
    ]
    production["ingress"]["host"] = coordinates["application_hostname"]
    production["platform"]["awsRegion"] = coordinates["aws_region"]
    production["platform"]["mlflow"]["databaseSecretArn"] = coordinates[
        "mlflow_database_secret_arn"
    ]
    production["platform"]["mlflow"]["artifactRoot"] = coordinates[
        "mlflow_artifact_root"
    ]
    production["platform"]["airflow"]["databaseSecretArn"] = coordinates[
        "airflow_database_secret_arn"
    ]
    production["platform"]["airflow"]["dataBucket"] = coordinates[
        "platform_data_bucket"
    ]
    production["platform"]["airflow"]["redisConnectionString"] = coordinates[
        "redis_connection_string"
    ]

    bootstrap = _load_yaml(bootstrap_values_path)
    addons = bootstrap["addons"]
    addons["awsLoadBalancerController"]["values"]["clusterName"] = coordinates[
        "cluster_name"
    ]
    addons["awsLoadBalancerController"]["values"]["region"] = coordinates[
        "aws_region"
    ]
    addons["clusterAutoscaler"]["values"]["autoDiscovery"][
        "clusterName"
    ] = coordinates["cluster_name"]
    addons["clusterAutoscaler"]["values"]["awsRegion"] = coordinates[
        "aws_region"
    ]
    addons["externalDns"]["values"]["domainFilters"] = [
        coordinates["domain_name"]
    ]
    addons["externalDns"]["values"]["txtOwnerId"] = coordinates["cluster_name"]

    _write_yaml(production_values_path, production)
    _write_yaml(bootstrap_values_path, bootstrap)


def _parse_args() -> argparse.Namespace:
    """Parse Terraform and Helm paths for deterministic value rendering."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-dir", type=Path, default=DEFAULT_TERRAFORM_DIR)
    parser.add_argument(
        "--production-values", type=Path, default=DEFAULT_PRODUCTION_VALUES
    )
    parser.add_argument(
        "--bootstrap-values", type=Path, default=DEFAULT_BOOTSTRAP_VALUES
    )
    return parser.parse_args()


def main() -> None:
    """Render checked AWS outputs into the GitOps deployment values."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    arguments = _parse_args()
    coordinates = load_coordinates(arguments.terraform_dir.resolve())
    render_values(
        coordinates,
        arguments.production_values.resolve(),
        arguments.bootstrap_values.resolve(),
    )


if __name__ == "__main__":
    main()
