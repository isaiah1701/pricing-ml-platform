"""Keep explicit model deployment inside existing approval and integrity gates."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from training.deployment import deploy_approved_version

LOGGER = logging.getLogger(__name__)


@pytest.mark.parametrize(
    ("approved", "valid_checksum"), [(True, True), (False, True), (True, False)]
)
def test_only_approved_version_can_be_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
    valid_checksum: bool,
) -> None:
    """Reject unapproved candidates and verify bytes before moving an approved alias."""
    current = SimpleNamespace(version="1")
    selected = SimpleNamespace(
        version="2",
        status="READY",
        run_id="run",
        tags={
            "promotion_status": "approved" if approved else "awaiting_approval",
            "model_artifact_sha256": "a" * 64,
        },
    )
    client = Mock()
    client.tracking_uri = "http://mlflow.test:5000"
    client.get_model_version_by_alias.return_value = current
    client.get_model_version.return_value = selected
    client.get_run.return_value = SimpleNamespace(
        info=SimpleNamespace(status="FINISHED"),
        data=SimpleNamespace(tags={"artifact_reload_verified": "true"}),
    )
    bundle = tmp_path / "release_bundle.json"
    bundle.write_text('{"model_uri":"models:/logged-model"}')
    client.download_artifacts.return_value = str(bundle)
    monkeypatch.setattr("training.deployment._require_registry_provenance", Mock())
    monkeypatch.setattr(
        "training.deployment.mlflow.artifacts.download_artifacts",
        Mock(return_value=str(tmp_path)),
    )
    monkeypatch.setattr(
        "training.deployment.sha256_directory",
        Mock(return_value=("a" if valid_checksum else "b") * 64),
    )
    persist = Mock()
    monkeypatch.setattr("training.deployment.persist_if_configured", persist)
    if not approved:
        with pytest.raises(ValueError, match="governed"):
            deploy_approved_version(client, "airbnb-price-predictor", "2")
        client.set_registered_model_alias.assert_not_called()
        persist.assert_not_called()
    elif not valid_checksum:
        with pytest.raises(ValueError, match="checksum"):
            deploy_approved_version(client, "airbnb-price-predictor", "2")
        client.set_registered_model_alias.assert_not_called()
        persist.assert_not_called()
    else:
        assert deploy_approved_version(client, "airbnb-price-predictor", "2") == "2"
        assert client.set_registered_model_alias.call_args_list[0].args == (
            "airbnb-price-predictor",
            "rollback",
            "1",
        )
        assert client.set_registered_model_alias.call_args_list[-1].args == (
            "airbnb-price-predictor",
            "champion",
            "2",
        )
        persist.assert_called_once()


def test_deployment_dag_is_manual_and_never_trains() -> None:
    """Expose model selection without creating a training dependency."""
    from pipelines.deploy import deployment_dag

    assert deployment_dag.schedule is None
    assert deployment_dag.task_ids == ["select_approved_model"]
