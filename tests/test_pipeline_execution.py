"""Offline checks for approved Airflow execution and repeatable preprocessing."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from scripts.trigger_airflow import run_pipeline as trigger
from training.preprocessing.pipeline import run_pipeline

LOGGER = logging.getLogger(__name__)


def test_airflow_submission_waits_for_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send one approved run and poll it without exposing the bearer token."""
    responses = [
        io.BytesIO(b"{}"),
        io.BytesIO(b'{"state":"running"}'),
        io.BytesIO(b'{"state":"success"}'),
    ]
    request = Mock(side_effect=responses)
    monkeypatch.setattr("scripts.trigger_airflow.urlopen", request)
    monkeypatch.setattr("scripts.trigger_airflow.time.sleep", Mock())
    trigger("https://airflow.internal", "secret", "airbnb_model_training", "ci-123")
    assert request.call_count == 3
    submitted = request.call_args_list[0].args[0]
    assert json.loads(submitted.data)["dag_run_id"] == "ci-123"
    assert submitted.get_header("Authorization") == "Bearer secret"


@pytest.mark.parametrize("state", ["failed", "queued"])
def test_airflow_failure_and_timeout_are_not_success(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Surface failed runs and finite polling timeouts to CI."""
    response = Mock(
        side_effect=[
            io.BytesIO(b"{}"),
            io.BytesIO(json.dumps({"state": state}).encode()),
        ]
    )
    monkeypatch.setattr("scripts.trigger_airflow.urlopen", response)
    monkeypatch.setattr("scripts.trigger_airflow.time.sleep", Mock())
    monkeypatch.setattr(
        "scripts.trigger_airflow.time.monotonic", Mock(side_effect=[0, 0, 2])
    )
    with pytest.raises(RuntimeError if state == "failed" else TimeoutError):
        trigger(
            "https://airflow.internal",
            "secret",
            "airbnb_feature_build",
            "ci-123",
            timeout=1,
        )


@pytest.mark.parametrize(
    ("url", "dag", "token"),
    [
        ("http://airflow.internal", "airbnb_feature_build", "secret"),
        ("https://airflow.internal", "unapproved_dag", "secret"),
        ("https://airflow.internal", "airbnb_model_training", ""),
    ],
)
def test_airflow_invalid_configuration_never_submits(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    dag: str,
    token: str,
) -> None:
    """Reject insecure or incomplete execution settings before network access."""
    request = Mock()
    monkeypatch.setattr("scripts.trigger_airflow.urlopen", request)
    with pytest.raises(ValueError):
        trigger(url, token, dag, "ci-123")
    request.assert_not_called()


def test_preprocessing_reuses_only_unchanged_valid_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse matching input/code, rebuild changed input and reject corrupted output."""
    source = tmp_path / "raw.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    output = tmp_path / "features.parquet"
    frame = pd.DataFrame({"feature": [1]})
    clean = Mock(return_value=frame)
    transform = Mock(return_value=frame)
    monkeypatch.setattr("training.preprocessing.pipeline.clean_listings", clean)
    monkeypatch.setattr("training.preprocessing.pipeline.transform_features", transform)
    monkeypatch.setattr("training.preprocessing.pipeline.OUTPUT_COLUMNS", ("feature",))
    counts = run_pipeline(source, output)
    assert run_pipeline(source, output) == counts
    assert clean.call_count == 1
    source.write_text("value\n2\n", encoding="utf-8")
    run_pipeline(source, output)
    assert clean.call_count == 2
    output.write_bytes(b"corrupt parquet")
    run_pipeline(source, output)
    assert clean.call_count == 3
