"""Validate the isolated autoscaling proposal without modifying the live cluster."""

import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def test_hpa_proposal_preserves_small_budget_and_owns_replicas() -> None:
    """Render an opt-in HPA against the actual Rollout and check conservative behavior."""
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "airbnb-inference",
            str(ROOT / "serving/helm"),
            "-f",
            str(ROOT / "serving/helm/values/production.yaml"),
            "-f",
            str(ROOT / "docs/proposals/autoscaling/serving-values.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources: list[dict[str, Any]] = [
        item for item in yaml.safe_load_all(rendered.stdout) if item
    ]
    hpa = next(item for item in resources if item["kind"] == "HorizontalPodAutoscaler")
    rollout = next(item for item in resources if item["kind"] == "Rollout")
    assert hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Rollout",
        "name": rollout["metadata"]["name"],
    }
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (1, 2)
    assert "replicas" not in rollout["spec"]
    assert hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 300
    assert hpa["spec"]["metrics"][0]["resource"]["name"] == "cpu"
    assert rollout["spec"]["template"]["spec"]["containers"][0]["resources"][
        "requests"
    ]["cpu"]
    bootstrap = yaml.safe_load(
        (ROOT / "serving/helm/values/bootstrap.yaml").read_text()
    )
    assert bootstrap["addons"]["metricsServer"]["enabled"] is True
    assert bootstrap["addons"]["clusterAutoscaler"]["enabled"] is False
    production = yaml.safe_load(
        (ROOT / "serving/helm/values/production.yaml").read_text()
    )
    assert production["autoscaling"]["enabled"] is False
