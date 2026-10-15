"""Persist privacy-reviewed prediction events for production drift monitoring."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Protocol

import boto3
from botocore.config import Config

from serving.app.config import Settings
from serving.app.schemas import ListingFeatures

LOGGER = logging.getLogger(__name__)


class PredictionEventRecorder(Protocol):
    """Describe a durable prediction-event sink."""

    def record(
        self,
        listing: ListingFeatures,
        *,
        prediction_id: str,
        prediction_timestamp: datetime,
        predicted_price: float,
        model_version: str,
    ) -> str:
        """Persist one prediction event and return its storage key."""
        ...


class S3PredictionEventRecorder:
    """Write immutable, time-partitioned prediction events to private S3."""

    def __init__(
        self,
        bucket: str,
        prefix: str,
        *,
        client: Any | None = None,
    ) -> None:
        """Create a recorder using bounded AWS SDK network timeouts."""
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client or boto3.client(
            "s3",
            config=Config(
                connect_timeout=1,
                read_timeout=2,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    def record(
        self,
        listing: ListingFeatures,
        *,
        prediction_id: str,
        prediction_timestamp: datetime,
        predicted_price: float,
        model_version: str,
    ) -> str:
        """Persist one schema-versioned event without user or credential data."""
        partition = prediction_timestamp.strftime("date=%Y-%m-%d/hour=%H")
        key = f"{self.prefix}/{partition}/{prediction_id}.json"
        event: dict[str, object] = {
            "schema_version": 1,
            "prediction_id": prediction_id,
            "prediction_timestamp": prediction_timestamp.isoformat(),
            "model_version": model_version,
            "prediction": round(predicted_price, 6),
            **listing.model_dump(),
        }
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        LOGGER.info("Persisted prediction event %s", prediction_id)
        return key


def create_prediction_event_recorder(
    settings: Settings,
) -> PredictionEventRecorder | None:
    """Create the configured event recorder or disable persistence locally."""
    if not settings.production_events_bucket.strip():
        LOGGER.info("Prediction-event persistence is disabled")
        return None
    return S3PredictionEventRecorder(
        settings.production_events_bucket,
        settings.production_events_prefix,
    )
