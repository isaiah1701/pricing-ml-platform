"""Unit tests for the single champion training pipeline."""

import logging
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from training.train import (
    ENTITY_COLUMNS,
    EXCLUDED_FEATURES,
    TARGET_COLUMN,
    CatBoostPriceEnsembleRegressor,
    build_training_dataset,
    evaluate_model,
    evaluate_segments,
    get_dataset_version,
)

LOGGER = logging.getLogger(__name__)


def test_estimator_import_does_not_require_training_only_dependencies() -> None:
    """Allow serving to unpickle the estimator without Feast or data tooling."""
    LOGGER.info("Checking estimator import in a serving-only environment")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.modules.update({name: None for name in "
                "('feast', 'pyarrow', 'training.lineage', 'training.provenance', 'training.validation')}); "
                "from training.train import CatBoostPriceEnsembleRegressor; "
                "assert CatBoostPriceEnsembleRegressor(feature_names=('latitude',)).feature_names == ('latitude',)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


FEATURE_NAMES: tuple[str, ...] = (
    "neighbourhood_group",
    "neighbourhood",
    "room_type",
    "minimum_nights",
)


@pytest.fixture
def aligned_training_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return labels and deliberately reordered historical features."""
    LOGGER.debug("Creating aligned training data")
    timestamps = pd.to_datetime(
        ["2019-07-08T00:00:00Z", "2019-07-08T00:00:00Z"], utc=True
    )
    labels = pd.DataFrame(
        {
            "id": [10, 20],
            "event_timestamp": timestamps,
            TARGET_COLUMN: [100.0, 200.0],
        }
    )
    features = pd.DataFrame(
        {
            "id": [20, 10],
            "event_timestamp": timestamps,
            "neighbourhood_group": ["Manhattan", "Brooklyn"],
            "neighbourhood": ["Harlem", "Bushwick"],
            "room_type": ["Entire home/apt", "Private room"],
            "minimum_nights": [2, 3],
        }
    )
    return labels, features


def test_training_dataset_aligns_and_excludes_metadata(
    aligned_training_data: tuple[pd.DataFrame, pd.DataFrame],
) -> None:
    """Align rows by keys without exposing entity metadata or target."""
    labels, features = aligned_training_data
    x, y = build_training_dataset(labels, features, FEATURE_NAMES)
    assert tuple(x.columns) == FEATURE_NAMES
    assert not {*ENTITY_COLUMNS, TARGET_COLUMN}.intersection(x.columns)
    assert x["minimum_nights"].tolist() == [3, 2]
    assert y.tolist() == [100.0, 200.0]


def test_training_dataset_rejects_target_leakage(
    aligned_training_data: tuple[pd.DataFrame, pd.DataFrame],
) -> None:
    """Reject a target unexpectedly returned among historical features."""
    labels, features = aligned_training_data
    features[TARGET_COLUMN] = [200.0, 100.0]
    with pytest.raises(ValueError, match="Unexpected price_per_night"):
        build_training_dataset(labels, features, FEATURE_NAMES)


def test_dataset_version_is_deterministic(tmp_path: Path) -> None:
    """Hash identical transformed dataset bytes to the same version."""
    data_path = tmp_path / "features.parquet"
    pd.DataFrame({"id": [1], "value": [2.0]}).to_parquet(data_path, index=False)
    first = get_dataset_version(data_path)
    assert first == get_dataset_version(data_path)
    assert len(first) == 64


def test_metric_calculation() -> None:
    """Calculate MAE, RMSE, and R-squared with expected values."""
    metrics = evaluate_model(pd.Series([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 4.0]))
    assert metrics["mae"] == pytest.approx(1.0 / 3.0)
    assert metrics["rmse"] == pytest.approx(np.sqrt(1.0 / 3.0))
    assert metrics["r2"] == pytest.approx(0.5)


def test_segment_metrics_keep_expensive_errors_in_headline() -> None:
    """Keep the full market primary while reporting diagnostics."""
    x = pd.DataFrame(
        {
            "room_type": [
                "Entire home/apt",
                "Private room",
                "Shared room",
                "Entire home/apt",
                "Private room",
                "Entire home/apt",
            ]
        }
    )
    y = pd.Series([100.0, 200.0, 250.0, 500.0, 600.0, 10000.0])
    predictions = np.array([110.0, 190.0, 230.0, 400.0, 450.0, 500.0])
    actual = evaluate_segments(x, y, predictions)
    expected = evaluate_model(y, predictions)
    assert all(actual[key] == pytest.approx(value) for key, value in expected.items())
    assert actual["ordinary_market_coverage"] == pytest.approx(4 / 6)
    assert actual["mae_above_500"] == pytest.approx(4825.0)


def test_champion_configuration_rejects_mismatched_members() -> None:
    """Keep redundant features out and require aligned ensemble members."""
    names = ("room_type", "availability_ratio", "neighbourhood_room_type")
    model = CatBoostPriceEnsembleRegressor(
        feature_names=names, random_states=(42, 7), iterations=(10,)
    )
    assert EXCLUDED_FEATURES == {"availability_ratio", "neighbourhood_room_type"}
    with pytest.raises(ValueError, match="equal length"):
        model.fit(pd.DataFrame({"room_type": ["Private room"]}), np.array([70.0]))
