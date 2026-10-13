"""Load and clean the NYC Airbnb training dataset."""

import logging
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger(__name__)
DEFAULT_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "AB_NYC_2019.csv"

REQUIRED_COLUMNS: tuple[str, ...] = (
    "id",
    "price",
    "neighbourhood_group",
    "neighbourhood",
    "latitude",
    "longitude",
    "room_type",
    "minimum_nights",
    "number_of_reviews",
    "calculated_host_listings_count",
    "availability_365",
)


def clean_listings(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return listings that satisfy the required training-data rules."""
    if not isinstance(dataframe, pd.DataFrame):
        LOGGER.error("Cleaning requires a pandas DataFrame")
        raise TypeError("dataframe must be a pandas DataFrame")

    if dataframe.columns.has_duplicates:
        LOGGER.error("Cleaning rejected duplicate input columns")
        raise ValueError("Input DataFrame contains duplicate column names")

    required_input_columns = set(REQUIRED_COLUMNS) | {
        "last_review",
        "reviews_per_month",
    }
    missing_columns = sorted(required_input_columns.difference(dataframe.columns))
    if missing_columns:
        LOGGER.error("Cleaning is missing columns: %s", missing_columns)
        raise ValueError(f"Missing required input columns: {missing_columns}")

    original_row_count = len(dataframe)
    LOGGER.info("Starting cleaning with %d raw rows", original_row_count)

    cleaned_data = dataframe.dropna(subset=list(REQUIRED_COLUMNS)).copy()
    missing_required_count = original_row_count - len(cleaned_data)
    LOGGER.info(
        "Required-field rule removed %d rows; nullable name, host_name, "
        "last_review, and reviews_per_month fields were preserved",
        missing_required_count,
    )

    no_reviews = pd.to_numeric(
        cleaned_data["number_of_reviews"], errors="coerce"
    ).eq(0)
    missing_reviews_per_month = cleaned_data["reviews_per_month"].isna()
    review_rate_fill_mask = no_reviews & missing_reviews_per_month
    cleaned_data.loc[review_rate_fill_mask, "reviews_per_month"] = 0.0
    LOGGER.info(
        "Review-rate rule filled reviews_per_month with 0.0 for %d "
        "listings with no reviews; last_review remained unchanged",
        int(review_rate_fill_mask.sum()),
    )

    numeric_price = pd.to_numeric(cleaned_data["price"], errors="coerce")
    valid_price = numeric_price.gt(0)
    invalid_price_count = int((~valid_price).sum())
    cleaned_data = cleaned_data.loc[valid_price].copy()
    cleaned_data["price"] = numeric_price.loc[valid_price]
    LOGGER.info(
        "Positive-price rule removed %d rows with invalid targets",
        invalid_price_count,
    )

    numeric_availability = pd.to_numeric(
        cleaned_data["availability_365"], errors="coerce"
    )
    valid_availability = numeric_availability.between(0, 365)
    invalid_availability_count = int((~valid_availability).sum())
    cleaned_data = cleaned_data.loc[valid_availability].copy()
    cleaned_data["availability_365"] = numeric_availability.loc[valid_availability]
    LOGGER.info(
        "Availability rule removed %d rows outside the inclusive 0..365 range",
        invalid_availability_count,
    )

    before_deduplication = len(cleaned_data)
    cleaned_data = cleaned_data.drop_duplicates(subset=["id"])
    duplicate_count = before_deduplication - len(cleaned_data)
    LOGGER.info("Listing-id rule removed %d duplicate rows", duplicate_count)

    cleaned_data = cleaned_data.reset_index(drop=True)
    removed_row_count = original_row_count - len(cleaned_data)
    LOGGER.info(
        "Cleaning completed: original row count=%d, final cleaned row count=%d, "
        "rows removed=%d",
        original_row_count,
        len(cleaned_data),
        removed_row_count,
    )
    return cleaned_data


def load_and_clean_data(data_path: str | Path = DEFAULT_DATA_PATH) -> pd.DataFrame:
    """Load a CSV dataset and return its cleaned listings."""
    resolved_path = Path(data_path)
    LOGGER.info("Loading dataset from %s", resolved_path)
    dataframe = pd.read_csv(resolved_path)
    return clean_listings(dataframe)


def main() -> None:
    """Load the default dataset and report a preview of the cleaned data."""
    cleaned_data = load_and_clean_data()
    LOGGER.info("Cleaned data preview:\n%s", cleaned_data.head().to_string(index=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
