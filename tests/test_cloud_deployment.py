"""Integration checks for the rendered AWS platform and production preflight."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from feast.infra.online_stores.redis import RedisOnlineStore, RedisOnlineStoreConfig
from feast.repo_config import load_repo_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.validate_aws_deployment import validate_values

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART = PROJECT_ROOT / "serving/helm"


@pytest.fixture(scope="module")
def platform() -> list[dict[str, Any]]:
    """Render the platform with realistic non-secret AWS coordinates."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    LOGGER.info("Rendering AWS platform for integration assertions")
    result = subprocess.run(
        [
            helm,
            "template",
            "airbnb-platform",
            str(CHART),
            "--namespace",
            "mlflow",
            "-f",
            str(CHART / "values/production.yaml"),
            "--set",
            "workload.enabled=false",
            "--set",
            "platform.enabled=true",
            "--set",
            "platform.mlflow.databaseHost=mlflow.internal",
            "--set",
            "platform.airflow.databaseHost=airflow.internal",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [resource for resource in yaml.safe_load_all(result.stdout) if resource]


def test_rds_secrets_only_fetch_managed_credential_properties(
    platform: list[dict[str, Any]],
) -> None:
    """Compose database URLs from Terraform endpoints and RDS credentials."""
    LOGGER.info("Checking RDS-managed secret compatibility")
    for component in ("mlflow", "airflow"):
        secret = next(
            r
            for r in platform
            if r["kind"] == "ExternalSecret"
            and r["metadata"]["name"] == f"{component}-database"
        )
        assert {item["remoteRef"]["property"] for item in secret["spec"]["data"]} == {
            "username",
            "password",
        }
        uri = next(iter(secret["spec"]["target"]["template"]["data"].values()))
        assert f"@{component}.internal:5432/{component}?sslmode=require" in uri
        assert ".password | urlquery" in uri
        if component == "airflow":
            asynchronous_uri = secret["spec"]["target"]["template"]["data"][
                "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_ASYNC"
            ]
            assert asynchronous_uri.startswith("postgresql+asyncpg://")
            assert asynchronous_uri.endswith("?ssl=require")


def test_airflow_async_database_url_uses_the_installed_tls_driver() -> None:
    """Ensure the async SQLAlchemy dialect accepts the PostgreSQL TLS option."""
    LOGGER.info("Checking Airflow's async PostgreSQL driver and TLS arguments")
    pytest.importorskip("asyncpg")
    url = "postgresql+asyncpg://test:test@db.internal:5432/airflow?ssl=require"
    engine = create_async_engine(url)
    _, arguments = engine.dialect.create_connect_args(make_url(url))
    assert arguments["ssl"] == "require"
    assert "sslmode" not in arguments


def test_airflow_runs_supervised_components_after_migrations(
    platform: list[dict[str, Any]],
) -> None:
    """Require separate processes, shared auth keys, and useful health probes."""
    LOGGER.info("Checking Airflow production process and storage contracts")
    deployment = next(
        r
        for r in platform
        if r["kind"] == "Deployment" and r["metadata"]["name"] == "airflow"
    )
    pod = deployment["spec"]["template"]["spec"]
    assert (
        deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"]
        == "airflow-database,airflow-runtime"
    )
    assert pod["nodeSelector"] == {"workload": "system"}
    config = next(
        r
        for r in platform
        if r["kind"] == "ConfigMap" and r["metadata"]["name"] == "airflow-runtime"
    )
    assert config["data"]["AIRFLOW__API__WORKERS"] == "1"
    assert config["data"]["AIRFLOW__DAG_PROCESSOR__PARSING_PROCESSES"] == "1"
    assert config["data"]["AIRFLOW__CORE__DAGBAG_IMPORT_TIMEOUT"] == "180"
    assert config["data"]["AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL"] == "300"
    mlflow = next(
        r
        for r in platform
        if r["kind"] == "Deployment" and r["metadata"]["name"] == "mlflow"
    )
    arguments = mlflow["spec"]["template"]["spec"]["containers"][0]["args"]
    assert mlflow["spec"]["strategy"] == {"type": "Recreate", "rollingUpdate": None}
    assert arguments[arguments.index("--workers") + 1] == "1"
    environment = {
        item["name"]: item.get("value")
        for item in mlflow["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["AWS_DEFAULT_REGION"] == environment["AWS_REGION"] == "eu-west-2"
    assert environment["MLFLOW_S3_ENDPOINT_URL"] == "https://s3.eu-west-2.amazonaws.com"
    assert len(environment) == len(
        mlflow["spec"]["template"]["spec"]["containers"][0]["env"]
    )
    assert (
        mlflow["spec"]["template"]["spec"]["containers"][0]["startupProbe"][
            "failureThreshold"
        ]
        == 60
    )
    allowed_hosts = arguments[arguments.index("--allowed-hosts") + 1]
    assert "mlflow.platf0rm.site" in allowed_hosts
    migration, data = pod["initContainers"]
    assert "airflow db migrate" in migration["args"][0]
    assert "airflow fab-db migrate" in migration["args"][0]
    assert "feature_store.aws.yaml" in data["args"][0]
    assert {c["name"] for c in pod["containers"]} == {
        "api-server",
        "scheduler",
        "dag-processor",
        "triggerer",
    }
    for container in pod["containers"]:
        assert container["command"] == ["airflow", container["name"]]
        assert {
            item["secretRef"]["name"]
            for item in container["envFrom"]
            if "secretRef" in item
        } == {"airflow-database", "airflow-runtime"}
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            assert "tcpSocket" not in container[probe]
            if container["name"] in ("dag-processor", "triggerer"):
                command = container[probe]["exec"]["command"]
                assert command[:2] == ["python", "-c"]
                assert "latest_heartbeat" in command[2]
                assert "socket.gethostname()" in command[2]
    runtime = next(
        r
        for r in platform
        if r["kind"] == "ExternalSecret" and r["metadata"]["name"] == "airflow-runtime"
    )
    assert runtime["spec"]["refreshPolicy"] == "CreatedOnce"
    keys = runtime["spec"]["target"]["template"]["data"]
    assert {
        "AIRFLOW__API_AUTH__JWT_SECRET",
        "AIRFLOW__CORE__FERNET_KEY",
        "AIRFLOW__API__SECRET_KEY",
        "AIRFLOW_ADMIN_PASSWORD",
    } <= keys.keys()


def test_public_observability_ingress_is_tls_and_mlflow_is_authenticated(
    platform: list[dict[str, Any]],
) -> None:
    """Expose Grafana and MLflow while requiring edge authentication for MLflow."""
    LOGGER.info("Checking public observability ingress controls")
    routes = {
        resource["metadata"]["name"]: resource
        for resource in platform
        if resource["kind"] == "IngressRoute"
    }
    certificates = {
        resource["metadata"]["name"]: resource
        for resource in platform
        if resource["kind"] == "Certificate"
    }
    mlflow_route = routes["mlflow-public"]
    assert mlflow_route["spec"]["routes"][0]["match"] == (
        "Host(`mlflow.platf0rm.site`)"
    )
    assert mlflow_route["spec"]["routes"][0]["middlewares"] == [
        {"name": "mlflow-public-auth"}
    ]
    assert certificates["mlflow-public"]["spec"]["dnsNames"] == [
        "mlflow.platf0rm.site"
    ]
    assert certificates["grafana-public"]["spec"]["dnsNames"] == [
        "grafana.platf0rm.site"
    ]
    external_secret = next(
        resource
        for resource in platform
        if resource["kind"] == "ExternalSecret"
        and resource["metadata"]["name"] == "mlflow-public-auth"
    )
    assert external_secret["spec"]["data"][0]["remoteRef"] == {
        "key": "prod-mlops-prod/mlflow-public-auth",
        "property": "users",
    }


@pytest.mark.parametrize("redis_type", ["redis", "redis_cluster"])
def test_aws_feast_configuration_selects_cluster_and_tls(
    monkeypatch: pytest.MonkeyPatch,
    redis_type: str,
) -> None:
    """Exercise Feast's standalone and cluster TLS client constructors offline."""
    LOGGER.info("Checking AWS Feast configuration against the locked client")
    monkeypatch.setenv("REDIS_CONNECTION_STRING", "cache.internal:6379,ssl=true")
    monkeypatch.setenv("REDIS_TYPE", redis_type)
    config = load_repo_config(
        PROJECT_ROOT / "feature_repo",
        PROJECT_ROOT / "feature_repo/feature_store.aws.yaml",
    )
    assert isinstance(config.online_store, RedisOnlineStoreConfig)
    client = Mock()
    constructor = Mock(return_value=client)
    client_class = "RedisCluster" if redis_type == "redis_cluster" else "Redis"
    monkeypatch.setattr(f"feast.infra.online_stores.redis.{client_class}", constructor)
    assert RedisOnlineStore()._get_client(config.online_store) is client
    options = constructor.call_args.kwargs
    assert options["ssl"] is True
    if redis_type == "redis_cluster":
        assert options["startup_nodes"][0].host == "cache.internal"
        assert int(options["startup_nodes"][0].port) == 6379
    else:
        assert options["host"] == "cache.internal"
        assert int(options["port"]) == 6379


def test_preflight_blocks_unrendered_production_values() -> None:
    """Reject placeholder endpoints and image tags before production sync."""
    LOGGER.info("Checking preflight rejects the unprovisioned production template")
    values = yaml.safe_load((CHART / "values/production.yaml").read_text())
    values["image"]["tag"] = "sha-REPLACE_WITH_GIT_SHA"
    with pytest.raises(ValueError, match="Unresolved or invalid"):
        validate_values(values)


def test_production_child_cannot_sync_placeholder_images() -> None:
    """Require Helm to reject the unrendered template when Argo enables its gate."""
    LOGGER.info("Checking the production Helm launch gate")
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    result = subprocess.run(
        [
            helm,
            "template",
            "airbnb-inference",
            str(CHART),
            "-f",
            str(CHART / "values/production.yaml"),
            "--set",
            "launchValidation=true",
            "--set",
            "image.tag=sha-REPLACE_WITH_GIT_SHA",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Launch requires rendered AWS values" in result.stderr


def test_preflight_accepts_coordinates_and_verifies_both_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require read-only ECR verification for both immutable image tags."""
    LOGGER.info("Checking the production preflight success path")
    image = {
        "repository": "123456789012.dkr.ecr.eu-west-2.amazonaws.com/serving",
        "tag": "sha-" + "a" * 40,
    }
    database = {
        "databaseSecretArn": "arn:aws:secretsmanager:eu-west-2:123456789012:secret:db",
        "databaseHost": "db.internal",
        "databasePort": 5432,
        "databaseName": "app",
    }
    values = {
        "image": image,
        "trainingImage": {
            **image,
            "repository": image["repository"].replace("/serving", "/training"),
        },
        "ingress": {"host": "platf0rm.site"},
        "platform": {
            "awsRegion": "eu-west-2",
            "mlflow": {**database, "artifactRoot": "s3://bucket/mlflow"},
            "airflow": {
                **database,
                "dataBucket": "bucket",
                "redisConnectionString": "cache.internal:6379,ssl=true",
            },
        },
    }
    helm = shutil.which("helm")
    if helm is not None:
        rendered = subprocess.run(
            [
                helm,
                "template",
                "airbnb-platform",
                str(CHART),
                "--namespace",
                "mlflow",
                "-f",
                "-",
                "--set",
                "launchValidation=true",
                "--set",
                "platform.enabled=true",
                "--set",
                "workload.enabled=false",
            ],
            input=yaml.safe_dump(values),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "kind: Deployment" in rendered.stdout
    command = Mock()
    monkeypatch.setattr("scripts.validate_aws_deployment.subprocess.run", command)
    validate_values(values, check_images=True)
    assert command.call_count == 2
    assert all("describe-images" in call.args[0] for call in command.call_args_list)
    values["platform"]["airflow"]["redisConnectionString"] = (
        "rediss://cache.internal:6379"
    )
    with pytest.raises(ValueError, match="Feast TLS format"):
        validate_values(values)
