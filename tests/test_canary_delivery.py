"""Rendered-manifest tests for automatic Argo Rollouts canary delivery."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = PROJECT_ROOT / "serving" / "helm"


@pytest.fixture(scope="module")
def rendered_resources() -> list[dict[str, Any]]:
    """Render production resources through Helm for semantic assertions."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    completed = subprocess.run(
        [
            helm,
            "template",
            "airbnb-inference",
            str(CHART_PATH),
            "--namespace",
            "airbnb-inference",
            "-f",
            str(CHART_PATH / "values" / "base.yaml"),
            "-f",
            str(CHART_PATH / "values" / "production.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [resource for resource in yaml.safe_load_all(completed.stdout) if resource]


def _resource(resources: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """Return one rendered resource by Kubernetes kind and name."""
    for resource in resources:
        if resource.get("kind") == kind and resource["metadata"]["name"] == name:
            return resource
    raise AssertionError(f"Missing {kind} {name}")


def test_rollout_starts_small_and_passing_canary_reaches_full_traffic(
    rendered_resources: list[dict[str, Any]],
) -> None:
    """Begin at five percent and include a final automatic 100-percent step."""
    rollout = _resource(rendered_resources, "Rollout", "airbnb-inference")
    canary = rollout["spec"]["strategy"]["canary"]
    weights = [step["setWeight"] for step in canary["steps"] if "setWeight" in step]
    assert weights == [5, 25, 50, 100]
    assert canary["stableService"] == "airbnb-inference"
    assert canary["canaryService"] == "airbnb-inference-canary"
    assert (
        canary["trafficRouting"]["traefik"]["weightedTraefikServiceName"]
        == "airbnb-inference"
    )


def test_failed_analysis_aborts_without_retargeting_stable_service(
    rendered_resources: list[dict[str, Any]],
) -> None:
    """Fail analysis promptly while Rollouts independently owns stable selectors."""
    analysis = _resource(
        rendered_resources, "AnalysisTemplate", "airbnb-inference-canary"
    )
    assert {metric["name"] for metric in analysis["spec"]["metrics"]} == {
        "prediction-error-rate",
        "prediction-p95-latency",
    }
    assert all(metric["failureLimit"] == 0 for metric in analysis["spec"]["metrics"])
    assert all(
        "result[0] <=" in metric["successCondition"]
        for metric in analysis["spec"]["metrics"]
    )
    assert all(
        "len(result) > 0 && !isNaN" in metric["successCondition"]
        for metric in analysis["spec"]["metrics"]
    )
    assert all(
        "len(result) == 0 || isNaN" in metric["failureCondition"]
        for metric in analysis["spec"]["metrics"]
    )
    assert all(
        ">= 20" in metric["provider"]["prometheus"]["query"]
        for metric in analysis["spec"]["metrics"]
    )
    rollout = _resource(rendered_resources, "Rollout", "airbnb-inference")
    assert rollout["spec"]["revisionHistoryLimit"] >= 2
    assert rollout["spec"]["strategy"]["canary"]["dynamicStableScale"] is False
    assert rollout["spec"]["strategy"]["canary"]["abortScaleDownDelaySeconds"] > 0
    stable = _resource(rendered_resources, "Service", "airbnb-inference")
    canary = _resource(rendered_resources, "Service", "airbnb-inference-canary")
    assert stable["spec"]["selector"] == canary["spec"]["selector"]
    assert stable["metadata"]["name"] != canary["metadata"]["name"]


def test_budget_rollout_uses_one_fixed_replica(
    rendered_resources: list[dict[str, Any]],
) -> None:
    """Keep the shared-node learning deployment at one serving replica."""
    LOGGER.info("Checking fixed serving capacity for the budget deployment")
    rollout = _resource(rendered_resources, "Rollout", "airbnb-inference")
    assert rollout["spec"]["replicas"] == 1
    assert not any(
        resource["kind"] == "HorizontalPodAutoscaler" for resource in rendered_resources
    )


def test_https_uses_cert_manager_secret_and_traefik(
    rendered_resources: list[dict[str, Any]],
) -> None:
    """Connect ACME issuance, TLS termination, and canary traffic routing."""
    LOGGER.info("Checking rendered certificate and TLS routing")
    certificate = _resource(rendered_resources, "Certificate", "airbnb-inference")
    issuer = _resource(rendered_resources, "Issuer", "letsencrypt")
    middleware = _resource(
        rendered_resources, "Middleware", "airbnb-inference-root-redirect"
    )
    route = _resource(rendered_resources, "IngressRoute", "airbnb-inference")
    weighted = _resource(rendered_resources, "TraefikService", "airbnb-inference")
    assert certificate["spec"]["dnsNames"] == ["platf0rm.site"]
    root_route, application_route = route["spec"]["routes"]
    assert root_route["match"] == "Host(`platf0rm.site`) && Path(`/`)"
    assert root_route["middlewares"] == [{"name": "airbnb-inference-root-redirect"}]
    assert application_route["match"] == "Host(`platf0rm.site`)"
    assert middleware["spec"]["redirectRegex"] == {
        "regex": "^https://[^/]+/?$",
        "replacement": "https://platf0rm.site/demo",
        "permanent": False,
    }
    bootstrap = yaml.safe_load(
        (CHART_PATH / "values/bootstrap.yaml").read_text(encoding="utf-8")
    )
    assert bootstrap["addons"]["externalDns"]["values"]["domainFilters"] == [
        "platf0rm.site"
    ]
    assert set(
        bootstrap["addons"]["traefik"]["values"]["service"]["annotations"][
            "external-dns.kubernetes.io/hostname"
        ].split(",")
    ) == {
        "platf0rm.site",
        "grafana.platf0rm.site",
        "mlflow.platf0rm.site",
        "argocd.platf0rm.site",
        "airflow.platf0rm.site",
    }
    assert certificate["spec"]["issuerRef"]["name"] == issuer["metadata"]["name"]
    assert route["spec"]["tls"]["secretName"] == certificate["spec"]["secretName"]
    assert (
        issuer["spec"]["acme"]["solvers"][0]["http01"]["ingress"]["ingressClassName"]
        == "traefik"
    )
    assert root_route["services"][0]["kind"] == "TraefikService"
    assert application_route["services"][0]["kind"] == "TraefikService"
    assert [
        service["name"] for service in weighted["spec"]["weighted"]["services"]
    ] == ["airbnb-inference", "airbnb-inference-canary"]


def test_gitops_preserves_rollouts_owned_routing_fields() -> None:
    """Scope drift exclusions to the exact fields the traffic controller mutates."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    rendered = subprocess.run(
        [
            helm,
            "template",
            "airbnb-gitops",
            str(CHART_PATH),
            "-f",
            str(CHART_PATH / "values/bootstrap.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = [item for item in yaml.safe_load_all(rendered.stdout) if item]
    app = _resource(resources, "Application", "airbnb-inference")
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]
    rules = app["spec"]["ignoreDifferences"]
    assert len(rules) == 3
    assert {rule["name"] for rule in rules if rule["kind"] == "Service"} == {
        "airbnb-inference",
        "airbnb-inference-canary",
    }
    assert all(rule["namespace"] == "airbnb-inference" for rule in rules)
    for rule in rules:
        if rule["kind"] == "Service":
            assert rule["jsonPointers"] == ["/spec/selector/rollouts-pod-template-hash"]
        else:
            assert rule["group"] == "traefik.io"
            assert rule["kind"] == "TraefikService"
            assert rule["name"] == "airbnb-inference"
            assert rule["jsonPointers"] == [
                "/spec/weighted/services/0/weight",
                "/spec/weighted/services/1/weight",
            ]
