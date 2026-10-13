"""Tests for challenger gates, governed promotion, and rollback behavior."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from retraining.retrain import (
    DeploymentCheckResult,
    deployment_checks_pass,
    evaluate_promotion_gates,
    promote_registered_challenger,
    require_approval,
    restore_previous_alias,
)

LOGGER = logging.getLogger(__name__)


class FakeRegistryClient:
    """Capture alias and tag operations without contacting MLflow."""

    def __init__(self, champion_version: str = "4") -> None:
        """Store the starting production version and an empty audit log."""
        self.champion_version = champion_version
        self.alias_calls: list[tuple[str, str, str]] = []
        self.tag_calls: list[tuple[str, str, str, str]] = []

    def get_model_version_by_alias(
        self, model_name: str, alias: str
    ) -> SimpleNamespace:
        """Return the configured current champion version."""
        del model_name, alias
        return SimpleNamespace(version=self.champion_version)

    def set_registered_model_alias(
        self, model_name: str, alias: str, version: str
    ) -> None:
        """Capture an alias mutation in call order."""
        self.alias_calls.append((model_name, alias, str(version)))

    def set_model_version_tag(
        self, model_name: str, version: str, key: str, value: str
    ) -> None:
        """Capture promotion or rollback audit metadata."""
        self.tag_calls.append((model_name, str(version), key, value))


def _metrics(mae: float, rmse: float, segment_mae: float) -> dict[str, float]:
    """Return the complete metric set required by promotion guardrails."""
    return {
        "mae": mae,
        "rmse": rmse,
        "mae_entire_home": segment_mae,
        "mae_private_room": segment_mae,
        "mae_ordinary_market": segment_mae,
    }


def _approval() -> dict[str, Any]:
    """Return an audited Airflow ApprovalOperator response."""
    return {
        "chosen_options": ["Approve"],
        "responded_by_user": "reviewer@example.com",
        "responded_at": "2026-09-29T14:00:00+00:00",
        "params_input": {},
    }


def test_rejected_challenger_records_all_failed_guardrails() -> None:
    """Reject insufficient MAE gain and RMSE/segment regressions."""
    champion = _metrics(mae=50.0, rmse=70.0, segment_mae=40.0)
    challenger = _metrics(mae=49.0, rmse=75.0, segment_mae=45.0)
    result = evaluate_promotion_gates(
        champion,
        challenger,
        minimum_mae_improvement=2.0,
        maximum_rmse_regression=0.02,
        maximum_segment_mae_regression=0.05,
    )
    assert result["passed"] is False
    assert any("MAE improvement" in reason for reason in result["rejection_reasons"])
    assert any("RMSE" in reason for reason in result["rejection_reasons"])
    assert any("mae_entire_home" in reason for reason in result["rejection_reasons"])


def test_approved_promotion_saves_rollback_before_alias_move() -> None:
    """Audit approval and preserve the old champion before promotion."""
    client = FakeRegistryClient(champion_version="4")
    result = promote_registered_challenger(
        client,
        "prices",
        "champion",
        "rollback",
        "5",
        _approval(),
    )
    assert result["previous_version"] == "4"
    assert result["promoted_version"] == "5"
    assert client.alias_calls == [
        ("prices", "rollback", "4"),
        ("prices", "champion", "5"),
    ]
    assert ("prices", "5", "approved_by", "reviewer@example.com") in client.tag_calls


def test_missing_approval_blocks_promotion() -> None:
    """Reject promotion when no complete human approval audit exists."""
    with pytest.raises(TypeError, match="human approval"):
        require_approval(None)
    with pytest.raises(ValueError, match="human approval"):
        require_approval({"chosen_options": ["Reject"]})


def test_failed_deployment_check_routes_to_rollback() -> None:
    """Treat health, version, latency, or model failures as rollback conditions."""
    result: DeploymentCheckResult = {
        "passed": False,
        "reasons": ["serving did not observe the promoted alias version"],
        "observed_version": "4",
        "p95_latency_seconds": None,
    }
    assert deployment_checks_pass(result) is False


def test_successful_rollback_restores_previous_alias() -> None:
    """Restore the saved version and mark the failed challenger rolled back."""
    client = FakeRegistryClient()
    restored = restore_previous_alias(
        client,
        {
            "model_name": "prices",
            "previous_version": "4",
            "promoted_version": "5",
            "approved_by": "reviewer@example.com",
            "approved_at": "2026-09-29T14:00:00+00:00",
        },
        "champion",
    )
    assert restored == "4"
    assert client.alias_calls == [("prices", "champion", "4")]
    assert ("prices", "5", "promotion_status", "rolled_back") in client.tag_calls


def test_successful_deployment_check_does_not_rollback() -> None:
    """Accept a healthy serving process that reports the promoted version."""
    result: DeploymentCheckResult = {
        "passed": True,
        "reasons": [],
        "observed_version": "5",
        "p95_latency_seconds": 0.04,
    }
    assert deployment_checks_pass(result) is True
