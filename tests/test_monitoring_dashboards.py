"""Static dashboard provisioning and producer/consumer metric contract checks."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.validate_monitoring import PROJECT_ROOT, validate_dashboards

LOGGER = logging.getLogger(__name__)


def test_dashboard_metrics_match_producers() -> None:
    """Validate every application metric reference and Helm copy."""
    assert validate_dashboards()
    copies = [
        yaml.safe_load((PROJECT_ROOT / relative).read_text())
        for relative in (
            "monitoring/prometheus/airbnb-alerts.yml",
            "serving/helm/files/airbnb-alerts.yml",
        )
    ]
    for copy in copies:
        for rule in copy["groups"][0]["rules"]:
            rule["expr"] = " ".join(rule["expr"].split())
    assert copies[0] == copies[1]


def test_misspelled_metric_is_rejected(tmp_path: Path) -> None:
    """Fail a meaningful dashboard typo even when both source and packaged copies agree."""
    for relative in ("monitoring/grafana", "serving/helm/files/dashboards"):
        shutil.copytree(PROJECT_ROOT / relative, tmp_path / relative)
    for relative in (
        "serving/app/api.py",
        "monitoring/drift_check.py",
        "monitoring/model_quality.py",
        "monitoring/operational_metrics.py",
        "serving/helm/values/bootstrap.yaml",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PROJECT_ROOT / relative, destination)
    for relative in ("monitoring/grafana/dashboards", "serving/helm/files/dashboards"):
        path = tmp_path / relative / "infrastructure-service-health.json"
        path.write_text(
            path.read_text().replace("airbnb_readiness", "airbnb_misspelled_readiness")
        )
    with pytest.raises(ValueError, match="Undefined application metrics"):
        validate_dashboards(tmp_path)


def test_dashboard_configmap_and_scrape_label_contract() -> None:
    """Require provisioned dashboard content and preservation of the app endpoint label."""
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "check",
            str(PROJECT_ROOT / "serving/helm"),
            "--namespace",
            "airbnb-inference",
            "-f",
            str(PROJECT_ROOT / "serving/helm/values/production.yaml"),
            "--set",
            "platform.enabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources: list[dict[str, Any]] = [
        item for item in yaml.safe_load_all(rendered.stdout) if item
    ]
    config = next(
        item
        for item in resources
        if item["kind"] == "ConfigMap"
        and item["metadata"]["name"] == "airbnb-mlops-dashboards"
    )
    assert config["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert len(config["data"]) == 5
    for name, contents in config["data"].items():
        assert json.loads(contents) == json.loads(
            (PROJECT_ROOT / "monitoring/grafana/dashboards" / name).read_text()
        )
    monitor = next(item for item in resources if item["kind"] == "ServiceMonitor")
    assert monitor["spec"]["endpoints"][0]["honorLabels"] is True
    bootstrap = yaml.safe_load(
        (PROJECT_ROOT / "serving/helm/values/bootstrap.yaml").read_text()
    )
    assert (
        bootstrap["addons"]["monitoring"]["values"]["grafana"]["sidecar"]["dashboards"][
            "enabled"
        ]
        is True
    )


def test_dashboard_views_keep_offline_quality_and_unknown_states_explicit() -> None:
    """Require all operator views and distinguish unavailable from healthy accuracy."""
    documents = [
        json.loads(path.read_text())
        for path in (PROJECT_ROOT / 'monitoring/grafana/dashboards').glob('*.json')
    ]
    assert {document['title'] for document in documents} == {
        'Inference Load Test', 'Model Inference', 'Model Quality & Drift',
        'Pipeline Health', 'Kubernetes & Delivery'
    }
    quality = next(document for document in documents if document['uid'] == 'airbnb-model-drift')
    delayed = next(panel for panel in quality['panels'] if panel['title'].startswith('Delayed outcome MAE'))
    assert 'airbnb_model_quality_mae_dollars' in delayed['targets'][0]['expr']
    assert 'offline_evaluation' not in delayed['targets'][0]['expr']
    assert delayed['fieldConfig']['defaults']['noValue'] == 'Awaiting source data'
    for panel in quality['panels']:
        if panel['title'].startswith('Offline holdout'):
            assert 'airbnb_offline_evaluation_' in panel['targets'][0]['expr']
    drift = next(panel for panel in quality['panels'] if panel['title'].startswith('Drift breach'))
    assert 'or vector(0)' not in drift['targets'][0]['expr']
