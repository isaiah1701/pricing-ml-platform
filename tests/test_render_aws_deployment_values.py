"""Tests for rendering Terraform outputs into production Helm values."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from scripts.render_aws_deployment_values import render_values

LOGGER = logging.getLogger(__name__)


def test_render_values_updates_workload_and_controller_coordinates(
    tmp_path: Path,
) -> None:
    """Write AWS endpoints and identities without placing credentials in Git."""
    production_path = tmp_path / "production.yaml"
    bootstrap_path = tmp_path / "bootstrap.yaml"
    production_path.write_text(
        yaml.safe_dump(
            {
                "image": {"repository": "old"},
                "trainingImage": {"repository": "old"},
                "ingress": {"host": "old.example"},
                "monitoring": {"productionEvents": {"bucket": "old"}},
                "platform": {
                    "awsRegion": "old",
                    "mlflow": {"databaseSecretArn": "old", "artifactRoot": "old"},
                    "airflow": {
                        "databaseSecretArn": "old",
                        "dataBucket": "old",
                        "redisConnectionString": "old",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    bootstrap_path.write_text(
        yaml.safe_dump(
            {
                "addons": {
                    "monitoring": {
                        "values": {
                            "alertmanager": {
                                "config": {
                                    "receivers": [
                                        {
                                            "name": "platform-sns",
                                            "sns_configs": [
                                                {
                                                    "topic_arn": "old",
                                                    "sigv4": {"region": "old"},
                                                }
                                            ],
                                        }
                                    ]
                                }
                            }
                        }
                    },
                    "traefik": {"values": {"service": {"annotations": {}}}},
                    "awsLoadBalancerController": {
                        "values": {"clusterName": "old", "region": "old"}
                    },
                    "clusterAutoscaler": {
                        "values": {
                            "autoDiscovery": {"clusterName": "old"},
                            "awsRegion": "old",
                        }
                    },
                    "externalDns": {
                        "values": {"domainFilters": [], "txtOwnerId": "old"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    coordinates = {
        "airflow_database_host": "airflow.internal",
        "airflow_database_port": "5432",
        "airflow_database_name": "airflow",
        "mlflow_database_host": "mlflow.internal",
        "mlflow_database_port": "5432",
        "mlflow_database_name": "mlflow",
        "airflow_database_secret_arn": "arn:airflow",
        "alert_topic_arn": "arn:aws:sns:eu-west-2:123:alerts",
        "application_hostname": "prices.example.com",
        "aws_region": "eu-west-2",
        "cluster_name": "prod-mlops-prod",
        "domain_name": "example.com",
        "mlflow_artifact_root": "s3://platform/mlflow",
        "mlflow_database_secret_arn": "arn:mlflow",
        "platform_data_bucket": "platform",
        "redis_connection_string": "redis:6379,ssl=true",
        "redis_type": "redis",
        "serving_repository": "registry/serving",
        "training_repository": "registry/training",
    }

    render_values(coordinates, production_path, bootstrap_path)

    production = yaml.safe_load(production_path.read_text(encoding="utf-8"))
    bootstrap = yaml.safe_load(bootstrap_path.read_text(encoding="utf-8"))
    assert production["image"]["repository"] == "registry/serving"
    assert production["platform"]["mlflow"]["databaseSecretArn"] == "arn:mlflow"
    assert production["platform"]["airflow"]["dataBucket"] == "platform"
    assert production["monitoring"]["productionEvents"]["bucket"] == "platform"
    assert production["platform"]["airflow"]["redisType"] == "redis"
    assert production["platform"]["mlflow"]["databaseHost"] == "mlflow.internal"
    assert production["platform"]["airflow"]["databaseName"] == "airflow"
    assert (
        bootstrap["addons"]["clusterAutoscaler"]["values"]["autoDiscovery"][
            "clusterName"
        ]
        == "prod-mlops-prod"
    )
    assert bootstrap["addons"]["externalDns"]["values"]["domainFilters"] == [
        "example.com"
    ]
    assert (
        bootstrap["addons"]["traefik"]["values"]["service"]["annotations"][
            "external-dns.kubernetes.io/hostname"
        ]
        == "prices.example.com,grafana.example.com,mlflow.example.com"
    )
    sns_config = bootstrap["addons"]["monitoring"]["values"]["alertmanager"][
        "config"
    ]["receivers"][0]["sns_configs"][0]
    assert sns_config == {
        "topic_arn": "arn:aws:sns:eu-west-2:123:alerts",
        "sigv4": {"region": "eu-west-2"},
    }
