"""Feature service declarations for Airbnb model versions."""

from feast import FeatureService
from feature_views import LISTING_FEATURE_NAMES, listing_features_view

airbnb_price_model_v1: FeatureService = FeatureService(
    name="airbnb_price_model_v1",
    features=[listing_features_view[list(LISTING_FEATURE_NAMES)]],
    description="Version 1 input contract for the Airbnb nightly-price model.",
)
