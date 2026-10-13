"""Data source declarations for the Airbnb feature repository."""

import os
from pathlib import Path

from feast import FileSource

FEATURE_DATA_PATH: Path = Path(
    os.getenv(
        "AIRBNB_FEATURE_DATA_PATH",
        Path(__file__).resolve().parents[1] / "data" / "listing_features.parquet",
    )
).expanduser().resolve()

listing_features_source: FileSource = FileSource(
    name="listing_features_source",
    path=str(FEATURE_DATA_PATH),
    timestamp_field="event_timestamp",
    description="Cleaned and transformed NYC Airbnb listing features.",
)
