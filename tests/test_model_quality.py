"""Tests for delayed production model-quality monitoring."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from monitoring.model_quality import run_model_quality_check

LOGGER = logging.getLogger(__name__)


def test_quality_check_publishes_overall_and_segment_metrics(tmp_path: Path) -> None:
    """Calculate interpretable regression metrics from valid matched outcomes."""
    outcomes = pd.DataFrame(
        {
            "prediction_id": ["p1", "p2", "p3", "p4"],
            "predicted_price": [100.0, 180.0, 500.0, 700.0],
            "actual_price": [100.0, 200.0, 400.0, 1000.0],
            "model_version": ["17", "17", "17", "17"],
            "outcome_timestamp": ["2026-10-09T12:00:00Z"] * 4,
            "room_type": [
                "Private room",
                "Private room",
                "Entire home/apt",
                "Entire home/apt",
            ],
            "neighbourhood_group": ["Manhattan", "Manhattan", "Brooklyn", "Brooklyn"],
        }
    )
    outcomes_path = tmp_path / "outcomes.parquet"
    outcomes.to_parquet(outcomes_path, index=False)

    result = run_model_quality_check(
        outcomes_path,
        tmp_path / "reports",
        tmp_path / "metrics" / "model_quality.prom",
        checked_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
    )

    overall = next(
        quality_slice
        for quality_slice in result["slices"]
        if quality_slice["segment"] == "overall"
    )
    assert overall["samples"] == 4
    assert overall["mae_dollars"] == pytest.approx(105.0)
    assert overall["mean_bias_dollars"] == pytest.approx(-55.0)
    assert overall["within_25_dollars_ratio"] == pytest.approx(0.5)
    assert overall["within_20_percent_ratio"] == pytest.approx(0.5)
    assert result["outcome_age_seconds"] == pytest.approx(86400.0)
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert report["matched_outcomes"] == 4
    metrics = Path(result["metrics_path"]).read_text(encoding="utf-8")
    assert 'airbnb_model_quality_mae_dollars{model_version="17",segment="overall",segment_value="all"} 105.0' in metrics
    assert 'airbnb_model_quality_samples{model_version="17",segment="room_type",segment_value="Private room"} 2.0' in metrics
    assert "airbnb_model_quality_mae_degradation_ratio" in metrics


def test_quality_check_excludes_invalid_and_duplicate_matches(tmp_path: Path) -> None:
    """Exclude ambiguous or invalid joins instead of corrupting quality metrics."""
    outcomes = pd.DataFrame(
        {
            "prediction_id": ["valid", "duplicate", "duplicate", "invalid"],
            "predicted_price": [100.0, 100.0, 110.0, -1.0],
            "actual_price": [110.0, 90.0, 90.0, 100.0],
            "model_version": ["18", "18", "18", "18"],
            "outcome_timestamp": ["2026-10-09T12:00:00Z"] * 4,
        }
    )
    outcomes_path = tmp_path / "outcomes.csv"
    outcomes.to_csv(outcomes_path, index=False)

    result = run_model_quality_check(
        outcomes_path,
        tmp_path / "reports",
        tmp_path / "quality.prom",
        checked_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
    )

    assert result["matched_outcomes"] == 1
    assert result["invalid_rows"] == 3
    assert result["duplicate_prediction_ids"] == 2
    assert result["match_ratio"] == pytest.approx(0.25)


def test_quality_check_requires_ground_truth_columns(tmp_path: Path) -> None:
    """Reject outcome files that cannot support supervised quality metrics."""
    outcomes_path = tmp_path / "outcomes.csv"
    pd.DataFrame({"prediction_id": ["p1"]}).to_csv(outcomes_path, index=False)

    with pytest.raises(ValueError, match="missing columns"):
        run_model_quality_check(
            outcomes_path,
            tmp_path / "reports",
            tmp_path / "quality.prom",
        )
