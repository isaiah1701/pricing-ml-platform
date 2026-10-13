"""Tests for the transformed training-data validation contract."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from training.preprocessing.transform import OUTPUT_COLUMNS
from training.validation import require_valid_dataset, validate_dataframe

LOGGER = logging.getLogger(__name__)


@pytest.fixture
def valid_dataset() -> pd.DataFrame:
    """Return one row satisfying the complete transformed-data contract."""
    row: dict[str, object] = {
        "id": 1,
        "event_timestamp": pd.Timestamp("2019-07-08T00:00:00Z"),
        "neighbourhood_group": "Brooklyn",
        "neighbourhood": "Bushwick",
        "latitude": 40.70,
        "longitude": -73.93,
        "room_type": "Private room",
        "minimum_nights": 2,
        "number_of_reviews": 5,
        "reviews_per_month": 1.2,
        "calculated_host_listings_count": 1,
        "availability_365": 90,
        "has_reviews": 1,
        "availability_ratio": 90.0 / 365.0,
        "days_since_last_review": 7,
        "minimum_nights_band": "short",
        "availability_band": "low",
        "location_cell": "40.70_-73.93",
        "distance_to_midtown_km": 8.0,
        "neighbourhood_room_type": "Bushwick__Private room",
        "price_per_night": 100,
    }
    frame = pd.DataFrame([row], columns=OUTPUT_COLUMNS)
    for column in (
        "id",
        "minimum_nights",
        "number_of_reviews",
        "calculated_host_listings_count",
        "availability_365",
        "has_reviews",
        "days_since_last_review",
        "price_per_night",
    ):
        frame[column] = frame[column].astype("int64")
    return frame


def test_valid_dataset_returns_machine_readable_report(
    valid_dataset: pd.DataFrame,
) -> None:
    """Accept a valid dataset and expose each rule result in a report."""
    report = validate_dataframe(valid_dataset)
    assert report["valid"] is True
    assert report["blocking_failures"] == []
    assert all(rule["passed"] for rule in report["rules"].values())


def test_invalid_dataset_is_deliberately_blocked(
    valid_dataset: pd.DataFrame, tmp_path: Path
) -> None:
    """Block invalid categories, coordinates, counts, nulls, and target values."""
    invalid = valid_dataset.copy()
    invalid.loc[0, "room_type"] = "Capsule"
    invalid.loc[0, "latitude"] = 91.0
    invalid.loc[0, "number_of_reviews"] = -1
    invalid.loc[0, "price_per_night"] = 20_000
    invalid.loc[0, "neighbourhood"] = None
    path = tmp_path / "blocked.parquet"
    invalid.to_parquet(path, index=False)

    report = validate_dataframe(invalid)
    assert report["valid"] is False
    assert {"nulls", "category_values", "coordinate_bounds", "non_negative_counts", "target_range"}.issubset(
        report["blocking_failures"]
    )
    with pytest.raises(ValueError, match="blocking validation rules"):
        require_valid_dataset(path)
