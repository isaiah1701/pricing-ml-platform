"""Read and display one listing's Feast features from Redis."""

import argparse
import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from feast import FeatureStore

from scripts.verify_feature_store import (
    DEFAULT_REDIS_CONNECTION,
    FEATURE_REPO_PATH,
    FEATURE_SERVICE_NAME,
    load_known_listing,
)

LOGGER = logging.getLogger(__name__)
ENTITY_JOIN_KEY: str = "id"


def has_materialized_features(features: dict[str, Any]) -> bool:
    """Return whether a response contains a non-null value beyond the entity key."""
    return any(
        value is not None
        for feature_name, value in features.items()
        if feature_name != ENTITY_JOIN_KEY
    )


def read_online_features(
    listing_id: int,
    repo_path: Path = FEATURE_REPO_PATH,
) -> dict[str, Any]:
    """Return Redis-backed model features for one listing identifier."""
    os.environ.setdefault("REDIS_CONNECTION_STRING", DEFAULT_REDIS_CONNECTION)
    LOGGER.info("Loading Feast repository from %s", repo_path)
    store = FeatureStore(repo_path=str(repo_path))
    feature_service = store.get_feature_service(FEATURE_SERVICE_NAME)
    response = store.get_online_features(
        features=feature_service,
        entity_rows=[{"id": listing_id}],
    ).to_dict()
    return {
        feature_name: values[0] if values else None
        for feature_name, values in sorted(response.items())
    }


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse an optional listing identifier from command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Display one listing's online Feast features from Redis."
    )
    parser.add_argument(
        "--listing-id",
        type=int,
        help="Listing id to retrieve; defaults to a known listing in the Parquet source.",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Retrieve and print one online feature record as JSON."""
    parsed_arguments = parse_args(arguments)
    listing_id = parsed_arguments.listing_id
    if listing_id is None:
        listing_id, _ = load_known_listing()

    LOGGER.info("Retrieving Redis-backed features for listing id=%d", listing_id)
    features = read_online_features(listing_id)
    if not has_materialized_features(features):
        LOGGER.error(
            "No online features found for listing id=%d; the id may not exist "
            "or may not have been materialized",
            listing_id,
        )
        return 1
    print(json.dumps(features, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
