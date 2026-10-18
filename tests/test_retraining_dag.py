"""Structural tests for event-triggered governed retraining."""

from __future__ import annotations

import logging

from airflow.providers.standard.operators.hitl import ApprovalOperator

from retraining.retrain import retraining_dag

LOGGER = logging.getLogger(__name__)


def test_retraining_dag_uses_native_approval_and_rollback_path() -> None:
    """Require HITL approval between challenger registration and promotion."""
    task_ids = set(retraining_dag.task_ids)
    assert retraining_dag.dag_id == "airbnb_model_retraining"
    assert {
        "load_retraining_event",
        "approve_expensive_training",
        "train_and_compare",
        "challenger_gate",
        "record_rejection",
        "register_challenger",
        "approve_production_promotion",
        "promote_to_production",
        "run_post_promotion_checks",
        "deployment_gate",
        "rollback_promotion",
        "promotion_succeeded",
    } == task_ids
    training_approval = retraining_dag.task_dict["approve_expensive_training"]
    assert isinstance(training_approval, ApprovalOperator)
    assert training_approval.upstream_task_ids == {"load_retraining_event"}
    assert "approve_expensive_training" in retraining_dag.task_dict[
        "train_and_compare"
    ].upstream_task_ids
    approval = retraining_dag.task_dict["approve_production_promotion"]
    assert isinstance(approval, ApprovalOperator)
    assert approval.upstream_task_ids == {"register_challenger"}
    assert retraining_dag.task_dict["promote_to_production"].upstream_task_ids == {
        "approve_production_promotion",
        "register_challenger",
    }
    assert retraining_dag.task_dict["rollback_promotion"].upstream_task_ids == {
        "deployment_gate",
        "promote_to_production",
    }
