"""Keep deterministic k6 samples compatible with the actual prediction contract."""

import json
import logging
from pathlib import Path

import pytest

from serving.app.schemas import MODEL_FEATURE_COLUMNS, ListingFeatures

LOGGER = logging.getLogger(__name__)
PAYLOADS = json.loads((Path(__file__).parent / "k6/listings.json").read_text())


@pytest.mark.parametrize("payload", PAYLOADS)
def test_k6_samples_satisfy_prediction_schema(payload: dict[str, object]) -> None:
    """Validate every realistic fixture against the serving schema and feature order."""
    features = ListingFeatures.model_validate(payload)
    assert tuple(features.to_model_frame().columns) == MODEL_FEATURE_COLUMNS
