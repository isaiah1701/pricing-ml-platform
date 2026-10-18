"""Run the local NYC Airbnb feature preprocessing pipeline."""

import hashlib
import json
import logging
from importlib.metadata import version
from pathlib import Path

import pandas as pd

from training.lineage import sha256_file
from training.preprocessing.clean import DEFAULT_DATA_PATH, clean_listings
from training.preprocessing.transform import OUTPUT_COLUMNS, transform_features

LOGGER = logging.getLogger(__name__)
DEFAULT_OUTPUT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "listing_features.parquet"
)


def run_pipeline(
    input_path: str | Path = DEFAULT_DATA_PATH,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> tuple[int, int, int, int]:
    """Clean, transform, persist, and validate the local feature dataset."""
    resolved_input = Path(input_path)
    resolved_output = Path(output_path)
    cache_path = resolved_output.with_suffix(resolved_output.suffix + ".build.json")
    source_files = [
        Path(__file__),
        Path(__file__).with_name("clean.py"),
        Path(__file__).with_name("transform.py"),
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "input": sha256_file(resolved_input),
                "code": [sha256_file(path) for path in source_files],
                "dependencies": {
                    name: version(name) for name in ("pandas", "numpy", "pyarrow")
                },
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if cache_path.is_file() and resolved_output.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached["fingerprint"] == fingerprint and cached[
                "output_sha256"
            ] == sha256_file(resolved_output):
                counts = cached["counts"]
                if len(counts) == 4 and all(type(count) is int for count in counts):
                    LOGGER.info(
                        "Reusing verified feature dataset at %s", resolved_output
                    )
                    return tuple(counts)
        except (ValueError, KeyError, TypeError):
            LOGGER.warning("Invalid preprocessing cache; rebuilding features")
    LOGGER.info("Reading raw NYC Airbnb data from %s", resolved_input)
    raw_data = pd.read_csv(resolved_input)
    raw_row_count = len(raw_data)

    cleaned_data = clean_listings(raw_data)
    transformed_data = transform_features(cleaned_data)

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    transformed_data.to_parquet(resolved_output, index=False)
    parquet_data = pd.read_parquet(resolved_output)

    if tuple(parquet_data.columns) != OUTPUT_COLUMNS:
        LOGGER.error("Persisted Parquet has an unexpected feature schema")
        raise ValueError("Persisted Parquet does not match the expected schema")
    if len(cleaned_data) != len(parquet_data):
        LOGGER.error(
            "Row-count mismatch: cleaned=%d, parquet=%d",
            len(cleaned_data),
            len(parquet_data),
        )
        raise ValueError("Cleaned and Parquet row counts do not match")

    LOGGER.info("Wrote and validated feature dataset at %s", resolved_output)
    counts = (
        raw_row_count,
        len(cleaned_data),
        len(parquet_data),
        len(parquet_data.columns),
    )
    cache_path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "output_sha256": sha256_file(resolved_output),
                "counts": counts,
            }
        ),
        encoding="utf-8",
    )
    return counts


def main() -> None:
    """Run preprocessing with project defaults and report validation counts."""
    raw_rows, cleaned_rows, parquet_rows, parquet_columns = run_pipeline()
    LOGGER.info("Raw CSV row count: %d", raw_rows)
    LOGGER.info("Cleaned row count: %d", cleaned_rows)
    LOGGER.info("Parquet row count: %d", parquet_rows)
    LOGGER.info("Parquet column count: %d", parquet_columns)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    main()
