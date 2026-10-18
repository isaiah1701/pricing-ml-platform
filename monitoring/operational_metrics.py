"""Persist bounded batch metrics in S3 and replace fixed Pushgateway jobs."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
from prometheus_client.core import CounterMetricFamily, Metric

LOGGER = logging.getLogger(__name__)
DAGS = frozenset({"airbnb_feature_build", "airbnb_model_training", "airbnb_model_deploy", "airbnb_drift_monitoring", "airbnb_model_quality_monitoring", "airbnb_model_retraining"})
EVENTS = {"promotion": "promotions", "rollback": "rollbacks", "rollback_failure": "rollback_failures", "retraining": "retraining_events"}

METRIC_NAMES = frozenset({
    "airbnb_pipeline_last_run_success", "airbnb_pipeline_last_success_timestamp_seconds",
    "airbnb_pipeline_last_duration_seconds", "airbnb_pipeline_failures_total",
    "airbnb_offline_evaluation_mae", "airbnb_offline_evaluation_rmse", "airbnb_offline_evaluation_r2",
    "airbnb_model_promotions_total", "airbnb_model_rollbacks_total", "airbnb_model_rollback_failures_total",
    "airbnb_model_retraining_events_total", "airbnb_model_last_promotion_timestamp_seconds",
    "airbnb_model_last_rollback_timestamp_seconds", "airbnb_drift_evaluation_failures_total",
})


@contextmanager
def _publication_lock(job: str) -> Iterator[None]:
    """Serialize durable updates and HTTP replacement across Airflow's shared PVC."""
    directory = Path(os.getenv("AIRBNB_BATCH_METRIC_STATE_DIR", "/tmp/airbnb-batch-metrics"))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{job}.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _bucket() -> str:
    """Read the existing platform bucket without constructing new infrastructure."""
    return os.getenv("PLATFORM_DATA_BUCKET", "").strip()


def _key(job: str) -> str:
    """Return the bounded latest-state key for a fixed batch job."""
    if job not in {"operational", "drift", "model-quality"}:
        raise ValueError("Unsupported operational metric job")
    return f"monitoring/metric-state/{job}/latest.json"


def _read(client: Any, bucket: str, key: str) -> tuple[dict[str, Any], str | None]:
    """Read a latest snapshot and its concurrency token; distinguish absent from denied."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read()), response["ETag"]
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return {"gauges": {}, "counters": {}, "events": {}}, None
        raise


DRIFT_METRIC_NAMES = frozenset({
    "airbnb_feature_drift_score", "airbnb_dataset_drift", "airbnb_drifted_features",
    "airbnb_drift_share", "airbnb_drift_threshold", "airbnb_feature_drift_threshold",
    "airbnb_prediction_drift", "airbnb_drift_last_success_timestamp_seconds",
    "airbnb_data_missing_ratio", "airbnb_data_unseen_category_ratio",
    "airbnb_data_quality_invalid_rows", "airbnb_production_window_rows",
    "airbnb_data_quality_valid_ratio", "airbnb_production_data_modified_timestamp_seconds",
})
QUALITY_METRIC_NAMES = frozenset({
    "airbnb_model_quality_mae_dollars", "airbnb_model_quality_rmse_dollars",
    "airbnb_model_quality_median_absolute_error_dollars", "airbnb_model_quality_mean_bias_dollars",
    "airbnb_model_quality_within_25_dollars_ratio", "airbnb_model_quality_within_20_percent_ratio",
    "airbnb_model_quality_samples", "airbnb_model_quality_r2", "airbnb_model_quality_mae_degradation_ratio",
    "airbnb_model_quality_match_ratio", "airbnb_model_quality_input_rows", "airbnb_model_quality_invalid_rows",
    "airbnb_model_quality_duplicate_prediction_ids", "airbnb_model_quality_latest_outcome_timestamp_seconds",
    "airbnb_model_quality_last_success_timestamp_seconds",
})


def _validate_state(state: dict[str, Any]) -> None:
    """Reject unknown names, arbitrary labels or excessive cardinality during recovery."""
    for kind in ("gauges", "counters"):
        for name, series in state.get(kind, {}).items():
            if name not in METRIC_NAMES | DRIFT_METRIC_NAMES | QUALITY_METRIC_NAMES:
                raise ValueError("Unknown retained metric name")
            allowed: set[str] = set()
            if name.startswith("airbnb_pipeline_"):
                allowed = {"dag"}
            elif name.startswith("airbnb_offline_"):
                allowed = {"role"}
            elif name == "airbnb_feature_drift_score":
                allowed = {"feature", "kind", "model_version"}
            elif name in {"airbnb_data_missing_ratio", "airbnb_data_unseen_category_ratio"}:
                allowed = {"feature"}
            elif name.startswith("airbnb_model_quality_"):
                if name in {"airbnb_model_quality_mae_dollars", "airbnb_model_quality_rmse_dollars",
                            "airbnb_model_quality_median_absolute_error_dollars", "airbnb_model_quality_mean_bias_dollars",
                            "airbnb_model_quality_within_25_dollars_ratio", "airbnb_model_quality_within_20_percent_ratio",
                            "airbnb_model_quality_samples", "airbnb_model_quality_r2"}:
                    allowed = {"model_version", "segment", "segment_value"}
                elif name == "airbnb_model_quality_mae_degradation_ratio":
                    allowed = {"model_version"}
            if len(series) > 256:
                raise ValueError("Retained metric exceeds bounded series limit")
            for item in series:
                labels = item["labels"]
                if set(labels) != allowed or any(not isinstance(value, str) or len(value) > 128 for value in labels.values()):
                    raise ValueError("Invalid retained metric labels")
                if labels.get("dag", "airbnb_feature_build") not in DAGS or labels.get("role", "champion") not in {"champion", "challenger"}:
                    raise ValueError("Unbounded retained DAG or role label")
                if not math.isfinite(float(item["value"])):
                    raise ValueError("Retained metric must be finite")


class _PersistedCounter:
    """Expose one retained counter family through the standard collector interface."""
    def __init__(self, family: CounterMetricFamily) -> None:
        """Keep a validated family without process-local counter resets."""
        self.family = family

    def collect(self) -> Iterator[Metric]:
        """Yield the durable cumulative counter samples."""
        yield self.family


def registry_for(state: dict[str, Any]) -> CollectorRegistry:
    """Build numeric metrics with explicitly bounded labels from a persisted snapshot."""
    _validate_state(state)
    registry = CollectorRegistry()
    for name, series in state.get("gauges", {}).items():
        labels = tuple(series[0]["labels"]) if series else ()
        gauge = Gauge(name, "Latest completed platform batch result.", labels, registry=registry)
        for item in series:
            gauge.labels(**item["labels"]).set(item["value"]) if labels else gauge.set(item["value"])
    for name, series in state.get("counters", {}).items():
        labels = tuple(series[0]["labels"]) if series else ()
        family = CounterMetricFamily(name.removesuffix("_total"), "Durable platform event count.", labels=labels)
        for item in series:
            family.add_metric([item["labels"][label] for label in labels], item["value"])
        registry.register(_PersistedCounter(family))
    return registry


def _push(job: str, state: dict[str, Any]) -> None:
    """Replace a fixed Pushgateway job; S3 remains authoritative if HTTP delivery fails."""
    gateway = os.getenv("PROMETHEUS_PUSHGATEWAY_URL", "").strip()
    if gateway:
        push_to_gateway(gateway, job=f"airbnb-{job}-monitoring" if job != "operational" else "airbnb-operational", registry=registry_for(state), timeout=10)


def _update_locked(job: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """CAS-update durable state before publishing, preventing concurrent event loss."""
    bucket = _bucket()
    if not bucket:
        LOGGER.info("Batch metric publication disabled: no platform bucket configured")
        return
    client = boto3.client("s3")
    key = _key(job)
    for _attempt in range(8):
        state, etag = _read(client, bucket, key)
        mutate(state)
        _validate_state(state)
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            client.put_object(Bucket=bucket, Key=key, Body=json.dumps(state, allow_nan=False).encode(), ContentType="application/json", ServerSideEncryption="AES256", **condition)
        except ClientError as error:
            if error.response["Error"]["Code"] in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                continue
            raise
        # Re-read instead of pushing an older CAS result after another publisher.
        _push(job, _read(client, bucket, key)[0])
        LOGGER.info("Persisted and published completed %s metrics", job)
        return
    raise RuntimeError("Concurrent operational state updates exceeded retry limit")


def update(job: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Serialize shared-PVC publishers through the durable write and gateway PUT."""
    _key(job)
    with _publication_lock(job):
        _update_locked(job, mutate)


def _set(state: dict[str, Any], name: str, value: float, labels: dict[str, str] | None = None) -> None:
    """Replace one bounded gauge series while preserving other configured roles/DAGs."""
    if not math.isfinite(value):
        raise ValueError("Operational metric values must be finite")
    label_values = labels or {}
    series = state.setdefault("gauges", {}).setdefault(name, [])
    series[:] = [item for item in series if item["labels"] != label_values]
    series.append({"labels": label_values, "value": value})


def _increment(state: dict[str, Any], name: str, labels: dict[str, str] | None = None) -> None:
    """Increment one durable counter without resetting it when a task process exits."""
    series = state.setdefault("counters", {}).setdefault(name, [])
    for item in series:
        if item["labels"] == (labels or {}):
            item["value"] += 1
            return
    series.append({"labels": labels or {}, "value": 1})


def _once(state: dict[str, Any], event_id: str) -> bool:
    """Deduplicate retry audit IDs durably without exposing them as metric labels."""
    digest = hashlib.sha256(event_id.encode()).hexdigest()
    events = state.setdefault("events", {})
    if digest in events:
        return False
    events[digest] = True
    return True


def _pipeline(context: dict[str, Any], success: bool) -> None:
    """Publish a terminal DAG outcome once per run and preserve previous successes."""
    run = context["dag_run"]
    dag = str(run.dag_id)
    if dag not in DAGS:
        LOGGER.error("Refusing metric labels for unknown DAG")
        raise ValueError("Unknown platform DAG")
    end = run.end_date or datetime.now(timezone.utc)
    timestamp = end.timestamp()
    duration = max(0.0, (end - (run.start_date or end)).total_seconds())
    def mutate(state: dict[str, Any]) -> None:
        """Apply only this terminal outcome, once, to shared durable state."""
        if not _once(state, f"pipeline:{dag}:{run.run_id}:{success}"):
            return
        completed = state.setdefault("pipeline_completed_at", {})
        if timestamp < completed.get(dag, 0):
            return
        completed[dag] = timestamp
        labels = {"dag": dag}
        _set(state, "airbnb_pipeline_last_run_success", float(success), labels)
        _set(state, "airbnb_pipeline_last_duration_seconds", duration, labels)
        if success:
            _set(state, "airbnb_pipeline_last_success_timestamp_seconds", timestamp, labels)
        else:
            _increment(state, "airbnb_pipeline_failures_total", labels)
    update("operational", mutate)
    if not success and dag == "airbnb_drift_monitoring":
        def failed(state: dict[str, Any]) -> None:
            """Count a failed drift run once while retaining its last healthy result."""
            if _once(state, str(run.run_id)):
                _increment(state, "airbnb_drift_evaluation_failures_total")
        update("drift", failed)


def pipeline_success(context: dict[str, Any]) -> None:
    """Airflow DAG callback publishing a completed successful pipeline."""
    _pipeline(context, True)


def pipeline_failure(context: dict[str, Any]) -> None:
    """Airflow DAG callback publishing a terminal failed pipeline."""
    _pipeline(context, False)


def publish_offline_evaluation(role: str, metrics: dict[str, float]) -> None:
    """Publish holdout metrics explicitly labelled as offline champion/challenger quality."""
    if role not in {"champion", "challenger"}:
        raise ValueError("Offline evaluation role must be champion or challenger")
    def mutate(state: dict[str, Any]) -> None:
        """Replace the chosen role's latest holdout measurements."""
        for name in ("mae", "rmse", "r2"):
            if name in metrics:
                _set(state, f"airbnb_offline_evaluation_{name}", float(metrics[name]), {"role": role})
    update("operational", mutate)


def publish_model_event(event: str, event_id: str, timestamp: float | None = None) -> None:
    """Count an idempotent audited lifecycle event after its verification completes."""
    if event not in EVENTS or not event_id:
        raise ValueError("Model event requires a supported type and stable audit ID")
    checked_at = time.time() if timestamp is None else timestamp
    def mutate(state: dict[str, Any]) -> None:
        """Apply an unseen lifecycle event to counters and completion timestamps."""
        if _once(state, f"model:{event}:{event_id}"):
            _increment(state, f"airbnb_model_{EVENTS[event]}_total")
            if event in {"promotion", "rollback"}:
                _set(state, f"airbnb_model_last_{event}_timestamp_seconds", checked_at)
    update("operational", mutate)


def persist_registry(job: str, registry: CollectorRegistry, timestamp: float) -> None:
    """Persist a completed result only when it is at least as recent as current state."""
    def mutate(state: dict[str, Any]) -> None:
        """Replace completed gauges while retaining failure counters and audit IDs."""
        if timestamp < state.get("completed_at", 0):
            return
        gauges: dict[str, Any] = {}
        for family in registry.collect():
            for sample in family.samples:
                if sample.name.endswith("_created"):
                    continue
                gauges.setdefault(sample.name, []).append({"labels": dict(sample.labels), "value": sample.value})
        state["gauges"] = gauges
        state["completed_at"] = timestamp
    update(job, mutate)


def main() -> None:
    """Replay durable latest states after Pushgateway/PVC recovery without dataset reads."""
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["replay"])
    parser.parse_args()
    bucket = _bucket()
    if not bucket:
        raise ValueError("PLATFORM_DATA_BUCKET is required for recovery replay")
    client = boto3.client("s3")
    for job in ("operational", "drift", "model-quality"):
        with _publication_lock(job):
            state, etag = _read(client, bucket, _key(job))
            if etag:
                _push(job, state)
                LOGGER.info("Replayed %s metric state", job)


if __name__ == "__main__":
    main()
