"""Tests for the Redis-backed feature inspection helpers."""

import logging

from scripts.read_online_features import has_materialized_features

LOGGER = logging.getLogger(__name__)


def test_materialized_response_requires_a_non_null_feature() -> None:
    """Do not mistake Feast's echoed entity key for an available feature row."""
    LOGGER.debug("Testing detection of an unknown online entity")
    assert not has_materialized_features({"id": 1, "availability_365": None})


def test_materialized_response_accepts_a_feature_value() -> None:
    """Accept an online response containing at least one model feature value."""
    LOGGER.debug("Testing detection of a materialized online entity")
    assert has_materialized_features({"id": 2539, "availability_365": 365})
