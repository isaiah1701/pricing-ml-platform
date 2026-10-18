"""Run the real serving HTTP layer with an explicit local-only fixture model."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from serving.app.api import create_app
from serving.app.config import Settings
from serving.app.model_loader import LoadedModel

LOGGER = logging.getLogger(__name__)


class FixtureModel:
    """Return a deterministic price without training or registry access."""

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Return one fixed response per validated input row."""
        return np.full(len(features), 100.0)


def load_fixture(settings: Settings) -> LoadedModel:
    """Identify the fixture clearly so it cannot be mistaken for a production model."""
    LOGGER.info(
        "Loading local k6 fixture for %s; no production model is used",
        settings.model_name,
    )
    return LoadedModel(FixtureModel(), "local-fixture", "fixture://k6")


app = create_app(
    settings=Settings(
        MODEL_NAME="k6-local-fixture",
        MODEL_REFRESH_INTERVAL_SECONDS=0,
        TRACING_ENABLED=False,
        PRODUCTION_EVENTS_BUCKET="",
        LOG_LEVEL="WARNING",
    ),
    model_loader=load_fixture,
)
