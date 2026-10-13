"""Validated HTTP contracts matching the champion's fitted feature schema."""

from __future__ import annotations

import logging
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger(__name__)

# This is the fitted estimator's feature_names_in_ order. The MLflow signature
# also contains two Feast columns that the estimator deliberately excludes.
MODEL_FEATURE_COLUMNS: tuple[str, ...] = (
    "neighbourhood_group",
    "neighbourhood",
    "latitude",
    "longitude",
    "room_type",
    "minimum_nights",
    "number_of_reviews",
    "reviews_per_month",
    "calculated_host_listings_count",
    "availability_365",
    "has_reviews",
    "days_since_last_review",
    "minimum_nights_band",
    "availability_band",
    "location_cell",
    "distance_to_midtown_km",
)


class ListingFeatures(BaseModel):
    """Represent one fully engineered row consumed by the fitted champion."""

    model_config = ConfigDict(extra="forbid")

    neighbourhood_group: str = Field(min_length=1)
    neighbourhood: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    room_type: Literal["Shared room", "Private room", "Entire home/apt"]
    minimum_nights: int = Field(ge=1)
    number_of_reviews: int = Field(ge=0)
    reviews_per_month: float = Field(ge=0.0)
    calculated_host_listings_count: int = Field(ge=1)
    availability_365: int = Field(ge=0, le=365)
    has_reviews: Literal[0, 1]
    days_since_last_review: int = Field(ge=-1)
    minimum_nights_band: Literal["short", "medium", "long", "extended"]
    availability_band: Literal[
        "unavailable", "low", "medium", "high", "very_high"
    ]
    location_cell: str = Field(min_length=1)
    distance_to_midtown_km: float = Field(ge=0.0)

    def to_model_frame(self) -> pd.DataFrame:
        """Return one row in the fitted estimator's exact feature order."""
        values = self.model_dump()
        return pd.DataFrame(
            [[values[column] for column in MODEL_FEATURE_COLUMNS]],
            columns=list(MODEL_FEATURE_COLUMNS),
        )


class HealthResponse(BaseModel):
    """Describe dependency-free process liveness."""

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """Describe the model currently ready to serve predictions."""

    status: Literal["ready"] = "ready"
    model: str
    alias: str
    version: str


class PredictionResponse(BaseModel):
    """Return a validated nightly-price prediction and model identity."""

    prediction_id: str = Field(min_length=1)
    prediction_timestamp: str = Field(min_length=1)
    predicted_price: float = Field(ge=0.0)
    model_name: str
    model_alias: str
    model_version: str
