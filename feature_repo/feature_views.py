"""Feature view declarations for Airbnb price prediction."""

from datetime import timedelta

from entities import listing
from feast import FeatureView, Field
from feast.types import Float64, Int32, Int64, String
from sources import listing_features_source

LISTING_FEATURE_NAMES: tuple[str, ...] = (
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
    "availability_ratio",
    "days_since_last_review",
    "minimum_nights_band",
    "availability_band",
    "location_cell",
    "distance_to_midtown_km",
    "neighbourhood_room_type",
)

listing_features_view: FeatureView = FeatureView(
    name="listing_features",
    entities=[listing],
    ttl=timedelta(days=3650),
    schema=[
        Field(name="neighbourhood_group", dtype=String),
        Field(name="neighbourhood", dtype=String),
        Field(name="latitude", dtype=Float64),
        Field(name="longitude", dtype=Float64),
        Field(name="room_type", dtype=String),
        Field(name="minimum_nights", dtype=Int64),
        Field(name="number_of_reviews", dtype=Int64),
        Field(name="reviews_per_month", dtype=Float64),
        Field(name="calculated_host_listings_count", dtype=Int64),
        Field(name="availability_365", dtype=Int64),
        Field(name="has_reviews", dtype=Int32),
        Field(name="availability_ratio", dtype=Float64),
        Field(name="days_since_last_review", dtype=Int64),
        Field(name="minimum_nights_band", dtype=String),
        Field(name="availability_band", dtype=String),
        Field(name="location_cell", dtype=String),
        Field(name="distance_to_midtown_km", dtype=Float64),
        Field(name="neighbourhood_room_type", dtype=String),
    ],
    source=listing_features_source,
    online=True,
    description="Predictive listing features for nightly-price regression.",
    tags={"domain": "airbnb", "model": "nightly_price"},
)
