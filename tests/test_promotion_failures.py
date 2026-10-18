"""Failure-path checks for durable registry promotion and verified rollback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from training import deployment

LOGGER = logging.getLogger(__name__)


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Use a small mutable registry while retaining the real receipt state machine."""
    monkeypatch.setenv("AIRBNB_PROMOTION_STATE_DIR", str(tmp_path))
    current = SimpleNamespace(version="4")
    client = Mock()
    client.get_model_version_by_alias.side_effect = lambda name, alias: current

    def set_alias(name: str, alias: str, version: str) -> None:
        """Apply concrete champion changes to the fixture registry."""
        if alias == "champion":
            current.version = version

    client.set_registered_model_alias.side_effect = set_alias
    monkeypatch.setattr(
        deployment,
        "verify_registry_version",
        Mock(return_value=SimpleNamespace(tags={"promotion_status": "approved"})),
    )
    monkeypatch.setattr(deployment, "_event", Mock())
    monkeypatch.setattr(deployment, "persist_if_configured", Mock())
    return client


def _begin(
    client: Mock, version: str = "5", operation: str = "review-1"
) -> dict[str, object]:
    """Begin one fully recorded human-approved operation."""
    return deployment.begin_promotion(
        client,
        "prices",
        version,
        operation_id=operation,
        approved_by="reviewer",
        approved_at="2026-10-18T10:00:00Z",
    )


def test_corrupt_rollback_candidate_blocks_promotion(
    registry: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified challenger cannot replace a champion whose backup bytes are corrupt."""
    monkeypatch.setattr(
        deployment,
        "verify_registry_version",
        Mock(side_effect=[Mock(), ValueError("checksum mismatch")]),
    )
    with pytest.raises(ValueError, match="checksum"):
        _begin(registry)
    registry.set_registered_model_alias.assert_not_called()


def test_expired_operation_cannot_be_replaced_or_complete(
    registry: Mock, tmp_path: Path
) -> None:
    """Expired receipts demand recovery rather than unsafe automatic lease takeover."""
    promotion = _begin(registry)
    receipt_path = next(tmp_path.glob("*.json"))
    receipt = json.loads(receipt_path.read_text())
    receipt["deadline"] = 1
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="lease expired"):
        _begin(registry)
    with pytest.raises(RuntimeError, match="Unfinished"):
        _begin(registry, "6", "review-2")
    with pytest.raises(RuntimeError, match="exceeded its lease"):
        deployment.complete_promotion(registry, promotion, serving_verified=True)
    assert (
        deployment.rollback_promotion_verified(
            registry, promotion, reason="lease expired", verifier=Mock()
        )
        == "4"
    )


def test_completed_promotion_cannot_be_rolled_back_by_stale_task(
    registry: Mock,
) -> None:
    """A delayed task cannot undo a later verified deployment."""
    first = _begin(registry)
    deployment.complete_promotion(registry, first, serving_verified=True)
    _begin(registry, "6", "review-2")
    with pytest.raises(RuntimeError, match="newer promotion"):
        deployment.rollback_promotion_verified(
            registry, first, reason="stale failure", verifier=Mock()
        )
    assert registry.get_model_version_by_alias("prices", "champion").version == "6"


def test_manual_deploy_failed_check_runs_verified_rollback(
    registry: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual ML Deploy shares the same saved-state and serving-verification path."""
    checker = Mock(side_effect=[RuntimeError("candidate unhealthy"), None])
    monkeypatch.setattr(deployment, "verify_serving_version", checker)
    with pytest.raises(RuntimeError, match="candidate unhealthy"):
        deployment.deploy_approved_version(
            registry, "prices", "5", operation_id="manual-1"
        )
    assert registry.get_model_version_by_alias("prices", "champion").version == "4"
    assert [call.args for call in checker.call_args_list] == [("5",), ("4",)]
    deployment._event.assert_called_once_with("rollback", "manual-1")


def test_rollback_failure_persists_recovery_state(
    registry: Mock, tmp_path: Path
) -> None:
    """No completion event is emitted while restored serving remains unhealthy."""
    promotion = _begin(registry)
    with pytest.raises(RuntimeError, match="unavailable"):
        deployment.rollback_promotion_verified(
            registry,
            promotion,
            reason="candidate failed",
            verifier=Mock(side_effect=RuntimeError("unavailable")),
        )
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["status"] == "rollback_failed"
    assert receipt["rollback_reason"] == "candidate failed"
    deployment._event.assert_called_once_with("rollback_failure", "review-1")
    with pytest.raises(RuntimeError, match="Unfinished"):
        _begin(registry, "6", "review-2")


def test_non_finite_comparison_metrics_block_promotion() -> None:
    """Reject NaN rather than letting comparison operators silently pass gates."""
    from retraining.retrain import evaluate_promotion_gates

    metrics = {"mae": float("nan"), "rmse": 1.0}
    assert not evaluate_promotion_gates(metrics, metrics, segment_metrics=())["passed"]


def test_model_event_failure_does_not_undo_verified_promotion(
    registry: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry outages retain a retryable outbox without causing model rollback."""
    promotion = _begin(registry)
    monkeypatch.setattr(deployment, "_event", Mock(return_value=False))
    deployment.complete_promotion(registry, promotion, serving_verified=True)
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["status"] == "completed"
    assert receipt["event_pending"] == [
        {"event": "promotion", "operation_id": "review-1"}
    ]
    assert registry.get_model_version_by_alias("prices", "champion").version == "5"
    monkeypatch.setattr(deployment, "_event", Mock(return_value=True))
    deployment.complete_promotion(registry, promotion, serving_verified=True)
    assert not json.loads(next(tmp_path.glob("*.json")).read_text())["event_pending"]


def test_model_event_failure_does_not_fail_verified_rollback(
    registry: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed rollback remains complete while notification telemetry is pending."""
    promotion = _begin(registry)
    monkeypatch.setattr(deployment, "_event", Mock(return_value=False))
    assert (
        deployment.rollback_promotion_verified(
            registry, promotion, reason="candidate unavailable", verifier=Mock()
        )
        == "4"
    )
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["status"] == "rolled_back"
    assert receipt["event_pending"] == [
        {"event": "rollback", "operation_id": "review-1"}
    ]


@pytest.mark.parametrize(
    "body", [{}, [], {"status": "ready", "alias": "champion", "version": "wrong"}]
)
def test_invalid_readiness_cannot_complete_promotion(
    body: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normalize malformed serving readiness into a failure that invokes rollback."""
    response = Mock()
    response.json.return_value = body
    monkeypatch.setattr(deployment.requests, "get", Mock(return_value=response))
    monkeypatch.setattr(deployment.time, "monotonic", Mock(side_effect=[0, 181]))
    with pytest.raises(RuntimeError, match="validation failed"):
        deployment.verify_serving_version("5")


def test_pending_event_survives_next_model_promotion(
    registry: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later successful releases preserve unsent earlier events and their audit IDs."""
    first = _begin(registry)
    monkeypatch.setattr(deployment, "_event", Mock(return_value=False))
    deployment.complete_promotion(registry, first, serving_verified=True)
    second = _begin(registry, "6", "review-2")
    deployment.complete_promotion(registry, second, serving_verified=True)
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert {entry["operation_id"] for entry in receipt["event_pending"]} == {
        "review-1",
        "review-2",
    }
    publisher = Mock(return_value=True)
    monkeypatch.setattr(deployment, "_event", publisher)
    deployment.complete_promotion(registry, second, serving_verified=True)
    assert {call.args for call in publisher.call_args_list} == {
        ("promotion", "review-1"),
        ("promotion", "review-2"),
    }
    assert not json.loads(next(tmp_path.glob("*.json")).read_text())["event_pending"]


def test_champion_quality_uses_real_run_metrics_after_success(
    registry: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh dashboard quality only from the successfully selected model's holdout."""
    registry.get_model_version.return_value = SimpleNamespace(run_id="approved-run")
    registry.get_run.return_value = SimpleNamespace(
        data=SimpleNamespace(metrics={"mae": 12.0, "rmse": 18.0})
    )
    publisher = Mock()
    monkeypatch.setattr(
        "monitoring.operational_metrics.publish_offline_evaluation", publisher
    )
    promotion = _begin(registry)
    deployment.complete_promotion(registry, promotion, serving_verified=True)
    publisher.assert_called_once_with("champion", {"mae": 12.0, "rmse": 18.0})
    publisher.side_effect = RuntimeError("telemetry unavailable")
    deployment.complete_promotion(registry, promotion, serving_verified=True)
    assert registry.get_model_version_by_alias("prices", "champion").version == "5"
