"""Typed environment configuration for model serving."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Runtime settings sourced exclusively from environment variables."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    mlflow_tracking_uri: str = Field(
        default="http://localhost:5000", validation_alias="MLFLOW_TRACKING_URI"
    )
    model_name: str = Field(
        default="airbnb-price-predictor", validation_alias="MODEL_NAME"
    )
    model_alias: str = Field(default="champion", validation_alias="MODEL_ALIAS")
    model_refresh_interval_seconds: float = Field(
        default=60.0, ge=0.0, validation_alias="MODEL_REFRESH_INTERVAL_SECONDS"
    )
    max_concurrent_predictions: int = Field(
        default=1, ge=1, le=32, validation_alias="MAX_CONCURRENT_PREDICTIONS"
    )
    prediction_queue_timeout_seconds: float = Field(
        default=5.0, gt=0.0, le=60.0, validation_alias="PREDICTION_QUEUE_TIMEOUT_SECONDS"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator(
        "mlflow_tracking_uri", "model_name", "model_alias", "log_level", mode="before"
    )
    @classmethod
    def reject_blank_values(cls, value: object) -> object:
        """Reject blank environment settings before application startup."""
        if isinstance(value, str) and not value.strip():
            LOGGER.error("A required serving setting was blank")
            raise ValueError("serving settings cannot be blank")
        return value.strip() if isinstance(value, str) else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one cached, validated settings object per process."""
    return Settings()
