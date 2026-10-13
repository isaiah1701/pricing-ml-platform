"""Verify matching offline and online values in the Airbnb feature store."""

import logging
import math
import os
from numbers import Real
from pathlib import Path
from typing import Any

import pandas as pd
from feast import FeatureStore
from redis.exceptions import ConnectionError as RedisConnectionError

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
FEATURE_REPO_PATH: Path = Path(
    os.getenv("AIRBNB_FEATURE_REPO_PATH", PROJECT_ROOT / "feature_repo")
).expanduser().resolve()
FEATURE_DATA_PATH: Path = Path(
    os.getenv(
        "AIRBNB_FEATURE_DATA_PATH",
        PROJECT_ROOT / "data" / "listing_features.parquet",
    )
).expanduser().resolve()
FEATURE_SERVICE_NAME: str = "airbnb_price_model_v1"
VERIFICATION_FEATURE: str = "availability_365"
DEFAULT_REDIS_CONNECTION: str = "localhost:6379"


def feature_values_match(offline_value: object, online_value: object) -> bool:
    """Return whether two non-null feature values are equivalent."""
    if offline_value is None or online_value is None:
        return False
    if isinstance(offline_value, Real) and isinstance(online_value, Real):
        return math.isclose(
            float(offline_value),
            float(online_value),
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
    return bool(offline_value == online_value)


def load_known_listing(data_path: Path = FEATURE_DATA_PATH) -> tuple[int, pd.Timestamp]:
    """Load one deterministic listing identifier and its event timestamp."""
    LOGGER.info("Loading a known listing from %s", data_path)
    listing_data = pd.read_parquet(
        data_path,
        columns=["id", "event_timestamp"],
    ).sort_values("id")
    if listing_data.empty:
        LOGGER.error("Feature dataset contains no listings")
        raise ValueError("Feature dataset contains no listings")

    listing_id = int(listing_data.iloc[0]["id"])
    event_timestamp = pd.Timestamp(listing_data.iloc[0]["event_timestamp"])
    return listing_id, event_timestamp


def verify_feature_store(repo_path: Path = FEATURE_REPO_PATH) -> None:
    """Assert that one feature agrees between historical and online retrieval."""
    os.environ.setdefault("REDIS_CONNECTION_STRING", DEFAULT_REDIS_CONNECTION)
    LOGGER.info("Loading Feast repository from %s", repo_path)
    store = FeatureStore(repo_path=str(repo_path))
    feature_service = store.get_feature_service(FEATURE_SERVICE_NAME)
    listing_id, event_timestamp = load_known_listing()

    entity_df = pd.DataFrame(
        {"id": [listing_id], "event_timestamp": [event_timestamp]}
    )
    LOGGER.info("Retrieving historical features for listing id=%d", listing_id)
    historical_data = store.get_historical_features(
        entity_df=entity_df,
        features=feature_service,
    ).to_df()
    if historical_data.empty:
        LOGGER.error("Historical retrieval returned no rows")
        raise ValueError("Historical retrieval returned no rows")
    offline_value: Any = historical_data.iloc[0][VERIFICATION_FEATURE]

    LOGGER.info("Retrieving online features for listing id=%d", listing_id)
    try:
        online_data = store.get_online_features(
            features=feature_service,
            entity_rows=[{"id": listing_id}],
        ).to_dict()
    except RedisConnectionError as exc:
        LOGGER.error("Could not connect to the configured Redis online store")
        raise ConnectionError(
            "Could not connect to Redis; start Redis and check "
            "REDIS_CONNECTION_STRING"
        ) from exc
    online_values = online_data.get(VERIFICATION_FEATURE, [])
    if not online_values:
        LOGGER.error("Online retrieval did not return %s", VERIFICATION_FEATURE)
        raise ValueError(
            f"Online retrieval did not return {VERIFICATION_FEATURE}"
        )
    online_value: Any = online_values[0]

    LOGGER.info("Offline %s value: %r", VERIFICATION_FEATURE, offline_value)
    LOGGER.info("Online %s value: %r", VERIFICATION_FEATURE, online_value)
    if not feature_values_match(offline_value, online_value):
        LOGGER.error(
            "Feature verification failed for listing id=%d: offline=%r, online=%r",
            listing_id,
            offline_value,
            online_value,
        )
        raise AssertionError("Offline and online feature values do not match")
    LOGGER.info("Feature-store verification succeeded for listing id=%d", listing_id)


def main() -> int:
    """Run feature-store verification and return a process exit code."""
    try:
        verify_feature_store()
    except Exception:
        LOGGER.exception("Feature-store verification failed")
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
