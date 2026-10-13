"""Unit tests for NYC Airbnb data cleaning."""

import logging

import pandas as pd
import pytest

from training.preprocessing.clean import clean_listings

LOGGER = logging.getLogger(__name__)


@pytest.fixture
def raw_data() -> pd.DataFrame:
    """Return listings that exercise every cleaning rule."""
    LOGGER.debug("Creating the raw-data cleaning fixture")
    return pd.DataFrame(
        {
            "id": [1, 2, 2, 3, 4, 5],
            "name": [None, "Valid", "Duplicate", "Free", "Unavailable", "Missing"],
            "host_name": [None, "Host", "Host", "Host", "Host", "Host"],
            "neighbourhood_group": ["Brooklyn"] * 5 + [None],
            "neighbourhood": ["Bushwick"] * 6,
            "latitude": [40.69] * 6,
            "longitude": [-73.92] * 6,
            "room_type": ["Private room"] * 6,
            "price": [75, 100, 100, 0, 90, 80],
            "minimum_nights": [1] * 6,
            "number_of_reviews": [0, 2, 2, 0, 1, 1],
            "last_review": [None, "2019-07-01", "2019-07-01", None, "2019-06-01", None],
            "reviews_per_month": [None, 1.0, 1.0, None, 0.5, None],
            "calculated_host_listings_count": [1] * 6,
            "availability_365": [365, 100, 100, 20, 366, 30],
        }
    )


def test_cleaning_rules_preserve_nullable_listing_metadata(
    raw_data: pd.DataFrame,
) -> None:
    """Preserve valid no-review listings while applying required filters."""
    LOGGER.debug("Testing required fields, target, availability, and duplicates")
    result = clean_listings(raw_data)

    assert result["id"].tolist() == [1, 2]
    assert pd.isna(result.loc[0, "name"])
    assert pd.isna(result.loc[0, "host_name"])
    assert pd.isna(result.loc[0, "last_review"])
    assert result.loc[0, "reviews_per_month"] == 0.0
    assert result["id"].is_unique
    assert result["price"].gt(0).all()
    assert result["availability_365"].between(0, 365).all()


def test_missing_review_rate_with_reviews_is_not_a_drop_reason(
    raw_data: pd.DataFrame,
) -> None:
    """Keep a reviewed listing even when its monthly review rate is unknown."""
    LOGGER.debug("Testing nullable reviews_per_month for reviewed listings")
    raw_data = raw_data.iloc[[1]].copy()
    raw_data["reviews_per_month"] = None

    result = clean_listings(raw_data)

    assert len(result) == 1
    assert pd.isna(result.loc[0, "reviews_per_month"])


def test_missing_cleaning_column_is_rejected(raw_data: pd.DataFrame) -> None:
    """Reject an input that cannot satisfy the cleaning contract."""
    LOGGER.debug("Testing rejection of a missing input column")

    with pytest.raises(ValueError, match="last_review"):
        clean_listings(raw_data.drop(columns="last_review"))
