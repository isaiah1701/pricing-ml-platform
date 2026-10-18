"""Airflow DAG that builds and publishes NYC Airbnb listing features."""

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import pandas as pd
from airflow.sdk import DAG, dag, task
from feast import FeatureStore

from training.lineage import (
    load_lineage_manifest,
    publish_dataset_snapshot,
    resolve_git_commit,
    verify_dataset_snapshot,
)
from training.preprocessing.pipeline import run_pipeline
from training.validation import ValidationReport, require_valid_dataset

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_REDIS_CONNECTION: str = "localhost:6379"
FEATURE_VIEW_NAME: str = "listing_features"


def _configured_path(environment_variable: str, default_path: Path) -> Path:
    """Resolve an optional environment-provided path against the process context."""
    configured_path = os.getenv(environment_variable)
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return default_path.resolve()


RAW_DATA_PATH: Path = _configured_path(
    "AIRBNB_RAW_DATA_PATH", PROJECT_ROOT / "data" / "AB_NYC_2019.csv"
)
FEATURE_DATA_PATH: Path = _configured_path(
    "AIRBNB_FEATURE_DATA_PATH",
    PROJECT_ROOT / "data" / "listing_features.parquet",
)
FEATURE_REPO_PATH: Path = _configured_path(
    "AIRBNB_FEATURE_REPO_PATH", PROJECT_ROOT / "feature_repo"
)
SNAPSHOT_ROOT: Path = _configured_path(
    "AIRBNB_SNAPSHOT_ROOT", PROJECT_ROOT / "data" / "snapshots"
)
VERIFY_SCRIPT_PATH: Path = _configured_path(
    "AIRBNB_VERIFY_SCRIPT_PATH", PROJECT_ROOT / "scripts" / "verify_feature_store.py"
)


class FeatureBuildResult(TypedDict):
    """Small XCom payload describing the generated feature artifact."""

    output_path: str
    raw_row_count: int
    cleaned_row_count: int
    row_count: int
    column_count: int
    event_timestamp_min: str
    event_timestamp_max: str


class ValidatedFeatureBuildResult(FeatureBuildResult):
    """Feature-build payload augmented with the blocking validation report."""

    validation_report: ValidationReport
    mutable_output_path: str
    snapshot_id: str
    dataset_sha256: str
    source_sha256: str
    lineage_id: str
    lineage_manifest_path: str


@task(retries=2, retry_delay=timedelta(minutes=5))
def build_feature_dataset() -> FeatureBuildResult:
    """Run the existing clean-and-transform pipeline and describe its output."""
    LOGGER.info(
        "Building Airbnb features with the existing preprocessing pipeline from %s",
        RAW_DATA_PATH,
    )
    if not RAW_DATA_PATH.is_file():
        LOGGER.error("Raw Airbnb dataset does not exist at %s", RAW_DATA_PATH)
        raise FileNotFoundError(f"Raw Airbnb dataset not found: {RAW_DATA_PATH}")

    (
        raw_row_count,
        cleaned_row_count,
        parquet_row_count,
        parquet_column_count,
    ) = run_pipeline(
        input_path=RAW_DATA_PATH,
        output_path=FEATURE_DATA_PATH,
    )
    persisted_features = pd.read_parquet(FEATURE_DATA_PATH)
    minimum_timestamp = pd.Timestamp(persisted_features["event_timestamp"].min())
    maximum_timestamp = pd.Timestamp(persisted_features["event_timestamp"].max())
    LOGGER.info(
        "Existing preprocessing pipeline wrote %d rows and %d columns to %s",
        parquet_row_count,
        parquet_column_count,
        FEATURE_DATA_PATH,
    )
    return {
        "output_path": str(FEATURE_DATA_PATH),
        "raw_row_count": raw_row_count,
        "cleaned_row_count": cleaned_row_count,
        "row_count": parquet_row_count,
        "column_count": parquet_column_count,
        "event_timestamp_min": minimum_timestamp.isoformat(),
        "event_timestamp_max": maximum_timestamp.isoformat(),
    }


@task(retries=0)
def validate_feature_dataset(
    feature_result: FeatureBuildResult,
) -> ValidatedFeatureBuildResult:
    """Validate and publish a write-once snapshot before Feast publication."""
    LOGGER.info("Validating transformed feature artifact before Feast publication")
    report = require_valid_dataset(feature_result["output_path"])
    published = publish_dataset_snapshot(
        feature_result["output_path"],
        SNAPSHOT_ROOT,
        source_path=RAW_DATA_PATH,
        validation_report=report,
        transformation_name="training.preprocessing.pipeline.run_pipeline",
        git_commit=resolve_git_commit(PROJECT_ROOT),
        feature_service_name="airbnb_price_model_v1",
        object_store_uri=os.getenv("AIRBNB_SNAPSHOT_S3_URI"),
    )
    immutable_report = require_valid_dataset(published["snapshot_path"])
    return {
        **feature_result,
        **published,
        "mutable_output_path": feature_result["output_path"],
        "output_path": published["snapshot_path"],
        "validation_report": immutable_report,
    }


@task(retries=1, retry_delay=timedelta(minutes=2))
def apply_feature_repository(
    feature_result: ValidatedFeatureBuildResult,
) -> ValidatedFeatureBuildResult:
    """Apply the Feast declarations after the feature artifact is validated."""
    feature_path = Path(feature_result["output_path"])
    if not feature_path.is_file():
        LOGGER.error("Feature artifact does not exist at %s", feature_path)
        raise FileNotFoundError(f"Feature artifact not found: {feature_path}")
    verify_dataset_snapshot(feature_path, feature_result["dataset_sha256"])
    load_lineage_manifest(feature_result["lineage_manifest_path"])

    feast_executable = shutil.which("feast")
    if feast_executable is None:
        LOGGER.error("The Feast CLI is not available to the Airflow worker")
        raise RuntimeError("The Feast CLI is not installed")

    environment = os.environ.copy()
    environment.setdefault("REDIS_CONNECTION_STRING", DEFAULT_REDIS_CONNECTION)
    environment["AIRBNB_FEATURE_DATA_PATH"] = str(feature_path)
    LOGGER.info("Applying Feast repository from %s", FEATURE_REPO_PATH)
    completed_process = subprocess.run(
        [feast_executable, "apply"],
        cwd=FEATURE_REPO_PATH,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed_process.returncode != 0:
        LOGGER.error("Feast apply failed: %s", completed_process.stderr.strip())
        raise RuntimeError("Feast repository apply failed")
    LOGGER.info("Feast repository applied successfully")
    return feature_result


@task(retries=2, retry_delay=timedelta(minutes=5))
def materialize_online_features(
    feature_result: ValidatedFeatureBuildResult,
) -> ValidatedFeatureBuildResult:
    """Materialize the generated snapshot into the configured Redis online store."""
    os.environ.setdefault("REDIS_CONNECTION_STRING", DEFAULT_REDIS_CONNECTION)
    os.environ["AIRBNB_FEATURE_DATA_PATH"] = feature_result["output_path"]
    verify_dataset_snapshot(
        feature_result["output_path"], feature_result["dataset_sha256"]
    )
    start_timestamp = (
        pd.Timestamp(feature_result["event_timestamp_min"]).to_pydatetime()
        - timedelta(seconds=1)
    )
    end_timestamp = (
        pd.Timestamp(feature_result["event_timestamp_max"]).to_pydatetime()
        + timedelta(seconds=1)
    )

    LOGGER.info(
        "Materializing %s from %s through %s",
        FEATURE_VIEW_NAME,
        start_timestamp,
        end_timestamp,
    )
    store = FeatureStore(repo_path=str(FEATURE_REPO_PATH))
    store.materialize(
        start_date=start_timestamp,
        end_date=end_timestamp,
        feature_views=[FEATURE_VIEW_NAME],
    )
    LOGGER.info("Online feature materialization completed")
    return feature_result


@task(retries=1, retry_delay=timedelta(minutes=2))
def verify_ready_features(feature_result: ValidatedFeatureBuildResult) -> None:
    """Run the existing offline-versus-online feature verification script."""
    LOGGER.info(
        "Verifying %d ready feature rows from %s",
        feature_result["row_count"],
        feature_result["output_path"],
    )
    environment = os.environ.copy()
    environment.setdefault("REDIS_CONNECTION_STRING", DEFAULT_REDIS_CONNECTION)
    environment["AIRBNB_FEATURE_DATA_PATH"] = feature_result["output_path"]
    environment["AIRBNB_LINEAGE_MANIFEST_PATH"] = feature_result[
        "lineage_manifest_path"
    ]
    verify_dataset_snapshot(
        feature_result["output_path"], feature_result["dataset_sha256"]
    )
    completed_process = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT_PATH)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    if completed_process.returncode != 0:
        LOGGER.error("Feature-store verification script failed")
        raise RuntimeError("Feature-store verification failed")
    LOGGER.info("Feature build completed and verified successfully")


@dag(
    dag_id="airbnb_feature_build",
    description="Clean, transform, register, materialize, and verify Airbnb features.",
    schedule=None,
    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    tags=["airbnb", "features", "feast"],
)
def build_feature_pipeline() -> None:
    """Define the ordered Airbnb feature-build workflow."""
    # Airflow replaces task return values with XComArg objects while constructing
    # the DAG, then resolves them to the annotated payloads when tasks execute.
    feature_result = build_feature_dataset()
    validated_result = validate_feature_dataset(feature_result)  # type: ignore[arg-type]
    applied_result = apply_feature_repository(validated_result)  # type: ignore[arg-type]
    materialized_result = materialize_online_features(
        applied_result  # type: ignore[arg-type]
    )
    verify_ready_features(materialized_result)  # type: ignore[arg-type]


feature_build_dag: DAG = build_feature_pipeline()
