"""Focused tests for safe champion registration helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mlflow.entities import Run

from scripts.register_model import find_logged_model, get_required_run_id
from training.registry import tag_version_before_alias

LOGGER = logging.getLogger(__name__)


class FakeAliasClient:
    """Record Registry writes and optionally fail one metadata operation."""

    def __init__(self, fail_key: str | None = None) -> None:
        """Store the optional failing tag and an ordered mutation log."""
        self.fail_key = fail_key
        self.calls: list[tuple[str, ...]] = []

    def set_model_version_tag(
        self, name: str, version: str, key: str, value: str
    ) -> None:
        """Record a tag write or raise before it can expose an alias."""
        self.calls.append(("tag", name, version, key, value))
        if key == self.fail_key:
            raise RuntimeError("tag write failed")

    def set_registered_model_alias(
        self, name: str, alias: str, version: str
    ) -> None:
        """Record the final alias publication."""
        self.calls.append(("alias", name, alias, version))


def test_champion_run_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject registration when no explicit champion run was selected."""
    monkeypatch.delenv("CHAMPION_RUN_ID", raising=False)
    with pytest.raises(ValueError, match="CHAMPION_RUN_ID is required"):
        get_required_run_id()


def test_champion_run_id_must_be_mlflow_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject malformed run identifiers before contacting MLflow."""
    monkeypatch.setenv("CHAMPION_RUN_ID", "not-a-run")
    with pytest.raises(ValueError, match="32-character hexadecimal"):
        get_required_run_id()


def test_find_logged_model_resolves_actual_mlflow3_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select the newest ready logged model named by the training code."""
    older = SimpleNamespace(
        name="model",
        status=SimpleNamespace(value="READY"),
        creation_timestamp=10,
        model_id="old",
    )
    newer = SimpleNamespace(
        name="model",
        status=SimpleNamespace(value="READY"),
        creation_timestamp=20,
        model_id="new",
    )

    def fake_search_logged_models(**kwargs: object) -> list[Any]:
        """Return two matching logged models without contacting MLflow."""
        del kwargs
        return [older, newer]

    monkeypatch.setattr(
        "scripts.register_model.mlflow.search_logged_models",
        fake_search_logged_models,
    )
    run = cast(
        Run,
        cast(
            Any,
            SimpleNamespace(info=SimpleNamespace(experiment_id="1", run_id="a" * 32)),
        ),
    )
    assert find_logged_model(run).model_id == "new"


def test_governance_tags_are_written_before_alias() -> None:
    """Expose a version only after every required metadata write succeeds."""
    client = FakeAliasClient()
    tag_version_before_alias(
        client,
        "prices",
        "7",
        "champion",
        {"release_bundle_id": "bundle", "dataset_sha256": "dataset"},
    )
    assert [call[0] for call in client.calls] == ["tag", "tag", "alias"]


def test_failed_governance_tag_never_moves_alias() -> None:
    """Leave aliases unchanged when any provenance tag cannot be persisted."""
    client = FakeAliasClient(fail_key="dataset_sha256")
    with pytest.raises(RuntimeError, match="tag write failed"):
        tag_version_before_alias(
            client,
            "prices",
            "7",
            "champion",
            {"release_bundle_id": "bundle", "dataset_sha256": "dataset"},
        )
    assert all(call[0] != "alias" for call in client.calls)
