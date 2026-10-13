"""Tests for feature-store verification helpers."""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.verify_feature_store import feature_values_match, load_known_listing

LOGGER = logging.getLogger(__name__)


def test_feature_values_match_numeric_types() -> None:
    """Treat equivalent numeric scalar representations as equal."""
    LOGGER.debug("Testing feature-value numeric comparison")
    assert feature_values_match(np.int64(365), 365)
    assert feature_values_match(np.float64(0.5), 0.5)


def test_feature_values_match_rejects_missing_or_different_values() -> None:
    """Reject missing online values and actual feature mismatches."""
    LOGGER.debug("Testing feature-value mismatch detection")
    assert not feature_values_match(365, None)
    assert not feature_values_match(365, 0)


def test_load_known_listing_uses_lowest_id(tmp_path: Path) -> None:
    """Select a stable listing independent of Parquet row order."""
    LOGGER.debug("Testing deterministic known-listing selection")
    data_path = tmp_path / "features.parquet"
    pd.DataFrame(
        {
            "id": [20, 10],
            "event_timestamp": pd.to_datetime(
                ["2019-07-08T00:00:00Z", "2019-07-08T00:00:00Z"], utc=True
            ),
        }
    ).to_parquet(data_path, index=False)

    listing_id, event_timestamp = load_known_listing(data_path)

    assert listing_id == 10
    assert event_timestamp == pd.Timestamp("2019-07-08T00:00:00Z")
