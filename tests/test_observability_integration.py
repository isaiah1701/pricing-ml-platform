"""Check cross-component observability and lifecycle boundaries without cloud access."""

from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def _module(relative: str) -> ModuleType:
    """Load an existing Airflow DAG, including its declared callback interfaces."""
    specification = importlib.util.spec_from_file_location("observability_dag", ROOT / relative)
    if specification is None or specification.loader is None:
        raise RuntimeError("DAG module could not be loaded")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_registration_preserves_existing_champion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful training evaluation registers a candidate without changing a live alias."""
    module = _module("pipelines/training.py")
    client = Mock()
    client.get_model_version_by_alias.return_value = SimpleNamespace(version="7")
    monkeypatch.setattr(module, "MlflowClient", Mock(return_value=client))
    monkeypatch.setattr(module.mlflow, "set_tracking_uri", Mock())
    monkeypatch.setattr(module.mlflow, "register_model", Mock(return_value=SimpleNamespace(version="8")))
    monkeypatch.setattr(module, "update_training_attempt", Mock())
    summary = Mock()
    monkeypatch.setattr(module, "record_training_attempt_summary", summary)
    alias = Mock()
    monkeypatch.setattr(module, "tag_version_before_alias", alias)
    backup = Mock()
    monkeypatch.setattr(module, "persist_if_configured", backup)
    candidate: dict[str, Any] = {
        "mae": 1.0, "rmse": 2.0, "r2": 0.9,
        "model_name": "catboost_price_ensemble_candidate",
        "artifact_reload_verified": True,
        "model_uri": "runs:/test/model", "run_id": "test",
        "training_attempt_id": "test", "release_bundle_id": "a" * 64,
        "code_sha256": "b" * 64, "dataset_sha256": "c" * 64,
        "model_artifact_sha256": "d" * 64,
    }
    result = module.conditional_registration.function(candidate)
    assert result["version"] == "8"
    alias.assert_not_called()
    client.set_registered_model_alias.assert_not_called()
    backup.assert_not_called()
    assert any(call.args[2:] == ("promotion_status", "approved") for call in client.set_model_version_tag.call_args_list)


@pytest.mark.parametrize(("relative", "attribute"), [
    ("pipelines/feature-build.py", "feature_build_dag"),
    ("pipelines/training.py", "training_dag"),
    ("pipelines/monitoring.py", "monitoring_dag"),
    ("pipelines/model_quality.py", "model_quality_dag"),
    ("pipelines/deploy.py", "deployment_dag"),
    ("retraining/retrain.py", "retraining_dag"),
])
def test_critical_dags_publish_terminal_outcomes(relative: str, attribute: str) -> None:
    """Every critical pipeline reports successful and failed terminal outcomes."""
    dag = getattr(_module(relative), attribute)
    assert dag.on_success_callback
    assert dag.on_failure_callback


def test_rollout_custom_metrics_have_real_collectors_and_rbac() -> None:
    """Require actual KSM extraction paths for dashboard/alert rollout series."""
    bootstrap = yaml.safe_load((ROOT / "serving/helm/values/bootstrap.yaml").read_text())
    settings = bootstrap["addons"]["monitoring"]["values"]["kube-state-metrics"]
    resources = settings["customResourceState"]["config"]["spec"]["resources"]
    produced = {f'{resource["metricNamePrefix"]}_{metric["name"]}' for resource in resources for metric in resource["metrics"]}
    assert produced == {"mlops_rollout_info", "mlops_rollout_stable_info", "mlops_rollout_aborted", "mlops_analysis_run_info"}
    assert all(resource["groupVersionKind"]["group"] == "argoproj.io" for resource in resources)
    assert "pods=[rollouts-pod-template-hash]" in settings["metricLabelsAllowlist"]
    rules = settings["rbac"]["extraRules"]
    assert any(set(rule["resources"]) == {"rollouts", "analysisruns"} for rule in rules)
    assert any("customresourcedefinitions" in rule["resources"] for rule in rules)


@pytest.mark.parametrize("failure", [False, True])
def test_drift_success_is_published_only_after_both_reports(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    """An incomplete durable report upload cannot advertise a completed evaluation."""
    module = _module("pipelines/monitoring.py")
    calls: list[str] = []

    def upload(*args: Any, **kwargs: Any) -> None:
        """Model report uploads, including a failure on the HTML object."""
        del kwargs
        calls.append(str(args[2]))
        if failure and len(calls) == 2:
            raise OSError("report upload unavailable")

    client = Mock()
    client.upload_file.side_effect = upload
    monkeypatch.setattr(module.boto3, "client", Mock(return_value=client))
    published = Mock(side_effect=lambda *args: calls.append("published"))
    monkeypatch.setattr(module, "write_prometheus_metrics", published)
    result = {
        "timestamp": "2026-10-18T03:00:00+00:00",
        "json_report_path": "/tmp/report.json", "html_report_path": "/tmp/report.html",
        "metrics_path": "/tmp/drift.prom",
    }
    if failure:
        with pytest.raises(OSError, match="report upload"):
            module.publish_drift_artifacts.function(result)
        published.assert_not_called()
    else:
        module.publish_drift_artifacts.function(result)
        assert len(calls) == 3
        assert calls[-1] == "published"
        assert calls[0].endswith("/report.json")
        assert calls[1].endswith("/report.html")


def test_grafana_synthetic_rules_match_provisioned_queries_and_producers() -> None:
    """Keep the executable alert fixtures tied to deployed queries and actual metric names."""
    from scripts.validate_monitoring import metric_names

    declared = metric_names(ROOT)
    configured = yaml.safe_load((ROOT / "monitoring/grafana/alert-rules.yml").read_text())["groups"][0]["rules"]
    fixtures = yaml.safe_load((ROOT / "tests/monitoring/grafana-rules.yml").read_text())["groups"][0]["rules"]
    synthetic = {rule["alert"]: rule for rule in fixtures}
    for rule in configured:
        query = rule["data"][0]["model"]["expr"]
        assert rule["data"][1]["model"]["expression"] == "$A > 0"
        expected = " ".join(f"({query}) > 0".split())
        assert " ".join(synthetic[rule["title"]]["expr"].split()) == expected
        assert synthetic[rule["title"]]["for"] == rule["for"]
        without_label_values = re.sub(r'"[^"\n]*"', '""', query)
        used = set(re.findall(r"\b(?:airbnb_|mlops_)[a-z0-9_]+", without_label_values))
        assert used <= declared
        runbook = rule["annotations"]["runbook_url"].split("/blob/main/", 1)[1]
        assert (ROOT / runbook).is_file()
