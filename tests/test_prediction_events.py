"""Tests for durable prediction events and S3 production-window materialization."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from monitoring.production_events import materialize_production_window
from serving.app.prediction_events import S3PredictionEventRecorder
from serving.app.schemas import ListingFeatures

LOGGER = logging.getLogger(__name__)


class FakePaginator:
    """Return fixed object keys from a fake S3 listing."""

    def __init__(self, keys: list[str]) -> None:
        """Store the keys returned by the paginator."""
        self.keys = keys

    def paginate(self, **kwargs: str) -> list[dict[str, object]]:
        """Return one page after validating the requested prefix."""
        assert kwargs == {"Bucket": "events", "Prefix": "production/events/"}
        return [{"Contents": [{"Key": key} for key in self.keys]}]


class FakeS3Client:
    """Capture S3 writes and serve in-memory event objects."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        """Initialize captured calls and optional readable objects."""
        self.objects = objects or {}
        self.put_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        """Capture one object write."""
        self.put_calls.append(kwargs)

    def get_paginator(self, name: str) -> FakePaginator:
        """Return the fixed list-objects paginator."""
        assert name == "list_objects_v2"
        return FakePaginator(sorted(self.objects))

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        """Return one in-memory event body."""
        assert Bucket == "events"
        return {"Body": io.BytesIO(self.objects[Key])}


def _listing() -> ListingFeatures:
    """Return one valid production listing event."""
    return ListingFeatures(
        neighbourhood_group="Manhattan",
        neighbourhood="Midtown",
        latitude=40.758,
        longitude=-73.9855,
        room_type="Entire home/apt",
        minimum_nights=2,
        number_of_reviews=10,
        reviews_per_month=1.25,
        calculated_host_listings_count=1,
        availability_365=120,
        has_reviews=1,
        days_since_last_review=30,
        minimum_nights_band="short",
        availability_band="medium",
        location_cell="40.76_-73.99",
        distance_to_midtown_km=0.0,
    )


def test_s3_recorder_writes_encrypted_partitioned_event() -> None:
    """Persist the complete drift schema without user identity data."""
    client = FakeS3Client()
    recorder = S3PredictionEventRecorder(
        "events", "production/events", client=client
    )
    timestamp = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)

    key = recorder.record(
        _listing(),
        prediction_id="prediction-1",
        prediction_timestamp=timestamp,
        predicted_price=123.4567899,
        model_version="7",
    )

    assert key == "production/events/date=2026-10-15/hour=09/prediction-1.json"
    call = client.put_calls[0]
    assert call["ServerSideEncryption"] == "AES256"
    event = json.loads(call["Body"])
    assert event["prediction"] == 123.45679
    assert event["model_version"] == "7"
    assert "user" not in event


def test_materialize_production_window_orders_and_bounds_events(
    tmp_path: Path,
) -> None:
    """Build the latest bounded Parquet window from immutable event objects."""
    objects: dict[str, bytes] = {}
    for index in range(3):
        event = {
            **_listing().model_dump(),
            "prediction": 100.0 + index,
            "prediction_id": f"prediction-{index}",
            "prediction_timestamp": f"2026-10-15T0{index}:00:00+00:00",
            "model_version": "7",
        }
        key = f"production/events/date=2026-10-15/hour=0{index}/prediction-{index}.json"
        objects[key] = json.dumps(event).encode()
    destination = tmp_path / "production.parquet"

    result = materialize_production_window(
        "events",
        "production/events",
        destination,
        window_rows=2,
        client=FakeS3Client(objects),
    )

    frame = pd.read_parquet(result)
    assert frame["prediction_id"].tolist() == ["prediction-1", "prediction-2"]
    assert frame["prediction"].tolist() == [101.0, 102.0]
