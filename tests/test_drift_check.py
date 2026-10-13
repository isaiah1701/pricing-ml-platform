"""Tests for Evidently drift reports and Prometheus metric publication."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from monitoring.drift_check import run_drift_check

LOGGER = logging.getLogger(__name__)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def reference_dataset(tmp_path: Path) -> Path:
    """Create a deterministic reference distribution for drift tests."""
    random = np.random.default_rng(42)
    rows = 400
    reference = pd.DataFrame(
        {
            "latitude": random.normal(40.72, 0.025, rows),
            "minimum_nights": random.integers(1, 8, rows),
            "room_type": random.choice(["Private room", "Shared room"], rows),
            "prediction": random.normal(150.0, 20.0, rows),
        }
    )
    path = tmp_path / "reference.parquet"
    reference.to_parquet(path, index=False)
    return path


def test_normal_sample_passes_without_drift(
    reference_dataset: Path, tmp_path: Path
) -> None:
    """Pass an unchanged production distribution and write all artifacts."""
    production_path = tmp_path / "normal.parquet"
    pd.read_parquet(reference_dataset).to_parquet(production_path, index=False)
    result = run_drift_check(
        reference_dataset,
        production_path,
        tmp_path / "reports",
        tmp_path / "metrics" / "drift.prom",
        numerical_features=("latitude", "minimum_nights"),
        categorical_features=("room_type",),
        production_window_rows=400,
        model_version="17",
    )
    assert result["dataset_drift"] is False
    assert result["drifted_feature_count"] == 0
    assert result["prediction_drift"] is False
    report = json.loads(Path(result["json_report_path"]).read_text(encoding="utf-8"))
    assert report["summary"]["model_version"] == "17"
    assert report["summary"]["timestamp"].endswith("+00:00")
    assert Path(result["html_report_path"]).is_file()
    metrics = Path(result["metrics_path"]).read_text(encoding="utf-8")
    assert "airbnb_feature_drift_score" in metrics
    assert "airbnb_dataset_drift 0.0" in metrics
    assert "airbnb_drift_last_success_timestamp_seconds" in metrics
    assert "airbnb_data_missing_ratio" in metrics
    assert "airbnb_data_unseen_category_ratio" in metrics
    assert "airbnb_data_quality_valid_ratio 1.0" in metrics


def test_deliberately_shifted_dataset_flags_features(
    reference_dataset: Path, tmp_path: Path
) -> None:
    """Flag shifted numerical, categorical, and prediction distributions."""
    result = run_drift_check(
        reference_dataset,
        FIXTURE_DIR / "drift_shifted.csv",
        tmp_path / "reports",
        tmp_path / "drift.prom",
        numerical_features=("latitude", "minimum_nights"),
        categorical_features=("room_type",),
        production_window_rows=40,
        drift_threshold=0.2,
        dataset_drift_share_threshold=0.5,
        model_version="18",
    )
    assert result["dataset_drift"] is True
    assert result["drifted_feature_count"] >= 3
    assert result["prediction_drift"] is True
    assert result["features"]["latitude"]["drifted"] is True
    assert result["features"]["room_type"]["drifted"] is True
