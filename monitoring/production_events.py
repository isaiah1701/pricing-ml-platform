"""Build a bounded drift-analysis window from durable S3 prediction events."""

from __future__ import annotations

import heapq
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import boto3
import pandas as pd

from serving.app.schemas import MODEL_FEATURE_COLUMNS

LOGGER = logging.getLogger(__name__)
REQUIRED_EVENT_COLUMNS: frozenset[str] = frozenset(
    (
        *MODEL_FEATURE_COLUMNS,
        "prediction",
        "prediction_id",
        "prediction_timestamp",
        "model_version",
    )
)


def materialize_production_window(
    bucket: str,
    prefix: str,
    destination: str | Path,
    *,
    window_rows: int = 10_000,
    client: Any | None = None,
) -> Path:
    """Download recent S3 events and atomically write a validated Parquet window."""
    if window_rows < 1:
        raise ValueError("window_rows must be positive")
    if not bucket.strip() or not prefix.strip("/"):
        raise ValueError("production-event bucket and prefix are required")
    s3 = client or boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    candidates: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=f"{prefix.strip('/')}/"
    ):
        keys = [
            str(item["Key"])
            for item in page.get("Contents", [])
            if str(item.get("Key", "")).endswith(".json")
        ]
        candidates = heapq.nlargest(window_rows * 2, [*candidates, *keys])
    if not candidates:
        LOGGER.error("No production prediction events were found in S3")
        raise ValueError("no production prediction events are available")

    records: list[dict[str, object]] = []
    for key in candidates:
        payload = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise TypeError(f"prediction event {key} was not an object")
        missing = sorted(REQUIRED_EVENT_COLUMNS.difference(decoded))
        if missing:
            LOGGER.error("Prediction event %s is missing columns %s", key, missing)
            raise ValueError(f"prediction event is missing columns: {missing}")
        records.append(decoded)

    frame = pd.DataFrame(records)
    frame["prediction_timestamp"] = pd.to_datetime(
        frame["prediction_timestamp"], utc=True, errors="raise"
    )
    frame = frame.sort_values("prediction_timestamp").tail(window_rows)
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".parquet", dir=target.parent, delete=False
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
    frame.to_parquet(temporary_path, index=False)
    os.replace(temporary_path, target)
    LOGGER.info("Materialized %d production events at %s", len(frame), target)
    return target
