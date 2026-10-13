"""Validate transformed Airbnb feature datasets before publication or training."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TypedDict

import numpy as np
import pandas as pd

from training.preprocessing.transform import OUTPUT_COLUMNS

LOGGER = logging.getLogger(__name__)

NYC_LATITUDE_RANGE: tuple[float, float] = (40.49, 40.92)
NYC_LONGITUDE_RANGE: tuple[float, float] = (-74.26, -73.69)
TARGET_RANGE: tuple[float, float] = (1.0, 10_000.0)
CATEGORY_VALUES: dict[str, frozenset[str]] = {
    "neighbourhood_group": frozenset(
        {"Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"}
    ),
    "room_type": frozenset(
        {"Entire home/apt", "Private room", "Shared room"}
    ),
    "minimum_nights_band": frozenset({"short", "medium", "long", "extended"}),
    "availability_band": frozenset(
        {"unavailable", "low", "medium", "high", "very_high"}
    ),
}
INTEGER_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "minimum_nights",
        "number_of_reviews",
        "calculated_host_listings_count",
        "availability_365",
        "has_reviews",
        "days_since_last_review",
    }
)
NUMERIC_COLUMNS: frozenset[str] = frozenset(
    {
        "latitude",
        "longitude",
        "reviews_per_month",
        "availability_ratio",
        "distance_to_midtown_km",
        "price_per_night",
        *INTEGER_COLUMNS,
    }
)
STRING_COLUMNS: frozenset[str] = frozenset(
    set(OUTPUT_COLUMNS) - NUMERIC_COLUMNS - {"event_timestamp"}
)
NON_NEGATIVE_COLUMNS: frozenset[str] = frozenset(
    {
        "minimum_nights",
        "number_of_reviews",
        "reviews_per_month",
        "calculated_host_listings_count",
        "availability_365",
        "has_reviews",
        "availability_ratio",
        "distance_to_midtown_km",
    }
)


class ValidationRule(TypedDict):
    """Machine-readable result for one blocking validation rule."""

    passed: bool
    blocking: bool
    failed_count: int
    details: dict[str, Any]


class ValidationReport(TypedDict):
    """Machine-readable aggregate transformed-dataset validation report."""

    valid: bool
    row_count: int
    column_count: int
    blocking_failures: list[str]
    rules: dict[str, ValidationRule]


def _rule(passed: bool, failed_count: int = 0, **details: Any) -> ValidationRule:
    """Build a consistently shaped blocking rule result."""
    return {
        "passed": bool(passed),
        "blocking": True,
        "failed_count": int(failed_count),
        "details": details,
    }


def validate_dataframe(dataframe: pd.DataFrame) -> ValidationReport:
    """Validate a transformed feature frame and return a serializable report."""
    if not isinstance(dataframe, pd.DataFrame):
        LOGGER.error("Dataset validation requires a pandas DataFrame")
        raise TypeError("dataframe must be a pandas DataFrame")

    LOGGER.info("Validating transformed dataset with %d rows", len(dataframe))
    required = set(OUTPUT_COLUMNS)
    missing = sorted(required.difference(dataframe.columns))
    unexpected = sorted(set(dataframe.columns).difference(required))
    duplicate_count = int(dataframe.columns.duplicated().sum())
    rules: dict[str, ValidationRule] = {
        "required_columns": _rule(
            not missing and duplicate_count == 0,
            len(missing) + duplicate_count,
            missing=missing,
            duplicate_columns=duplicate_count,
        ),
        "unexpected_columns": _rule(
            not unexpected, len(unexpected), unexpected=unexpected
        ),
        "non_empty": _rule(not dataframe.empty, int(dataframe.empty)),
    }

    available = required.intersection(dataframe.columns)
    null_counts = {
        name: int(dataframe[name].isna().sum())
        for name in sorted(available)
        if dataframe[name].isna().any()
    }
    rules["nulls"] = _rule(not null_counts, sum(null_counts.values()), columns=null_counts)

    invalid_types: dict[str, str] = {}
    for name in sorted(available):
        series = dataframe[name]
        if name in INTEGER_COLUMNS and not pd.api.types.is_integer_dtype(series):
            invalid_types[name] = "integer"
        elif name in NUMERIC_COLUMNS and not pd.api.types.is_numeric_dtype(series):
            invalid_types[name] = "numeric"
        elif name == "event_timestamp" and not pd.api.types.is_datetime64_any_dtype(
            series
        ):
            invalid_types[name] = "datetime"
        elif name in STRING_COLUMNS and not series.dropna().map(
            lambda value: isinstance(value, str)
        ).all():
            invalid_types[name] = "string"
    rules["types"] = _rule(
        not invalid_types, len(invalid_types), expected=invalid_types
    )

    invalid_categories: dict[str, list[str]] = {}
    for name, allowed in CATEGORY_VALUES.items():
        if name in available:
            values = set(dataframe[name].dropna().astype(str).unique())
            invalid = sorted(values.difference(allowed))
            if invalid:
                invalid_categories[name] = invalid
    rules["category_values"] = _rule(
        not invalid_categories,
        sum(len(values) for values in invalid_categories.values()),
        invalid=invalid_categories,
    )

    coordinate_failures: dict[str, int] = {}
    for name, bounds in (
        ("latitude", NYC_LATITUDE_RANGE),
        ("longitude", NYC_LONGITUDE_RANGE),
    ):
        if name in available and pd.api.types.is_numeric_dtype(dataframe[name]):
            numeric = dataframe[name].astype("float64")
            coordinate_failures[name] = int(
                (~np.isfinite(numeric) | ~numeric.between(*bounds)).sum()
            )
    coordinate_failures = {
        name: count for name, count in coordinate_failures.items() if count
    }
    rules["coordinate_bounds"] = _rule(
        not coordinate_failures,
        sum(coordinate_failures.values()),
        invalid=coordinate_failures,
        latitude=list(NYC_LATITUDE_RANGE),
        longitude=list(NYC_LONGITUDE_RANGE),
    )

    negative_counts: dict[str, int] = {}
    for name in sorted(NON_NEGATIVE_COLUMNS.intersection(available)):
        if pd.api.types.is_numeric_dtype(dataframe[name]):
            count = int((dataframe[name] < 0).sum())
            if count:
                negative_counts[name] = count
    rules["non_negative_counts"] = _rule(
        not negative_counts, sum(negative_counts.values()), invalid=negative_counts
    )

    target_failures = 0
    if "price_per_night" in available and pd.api.types.is_numeric_dtype(
        dataframe["price_per_night"]
    ):
        target = dataframe["price_per_night"].astype("float64")
        target_failures = int(
            (~np.isfinite(target) | ~target.between(*TARGET_RANGE)).sum()
        )
    rules["target_range"] = _rule(
        target_failures == 0,
        target_failures,
        minimum=TARGET_RANGE[0],
        maximum=TARGET_RANGE[1],
    )

    blocking_failures = [name for name, result in rules.items() if not result["passed"]]
    if blocking_failures:
        LOGGER.error("Dataset validation failed: %s", blocking_failures)
    else:
        LOGGER.info("Dataset validation passed")
    return {
        "valid": not blocking_failures,
        "row_count": len(dataframe),
        "column_count": len(dataframe.columns),
        "blocking_failures": blocking_failures,
        "rules": rules,
    }


def validate_dataset(data_path: str | Path) -> ValidationReport:
    """Load a Parquet feature dataset and return its validation report."""
    resolved_path = Path(data_path)
    if not resolved_path.is_file():
        LOGGER.error("Validation dataset does not exist: %s", resolved_path)
        raise FileNotFoundError(f"Feature dataset not found: {resolved_path}")
    return validate_dataframe(pd.read_parquet(resolved_path))


def require_valid_dataset(data_path: str | Path) -> ValidationReport:
    """Return the report or raise when any blocking validation rule fails."""
    report = validate_dataset(data_path)
    if not report["valid"]:
        failures = ", ".join(report["blocking_failures"])
        raise ValueError(f"Dataset failed blocking validation rules: {failures}")
    return report
