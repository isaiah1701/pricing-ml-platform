"""Verify durable publication, retry idempotency and freshness without AWS writes."""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from monitoring import operational_metrics as metrics

LOGGER = logging.getLogger(__name__)


class MemoryS3:
    """Model conditional S3 writes and process-independent retained objects."""
    def __init__(self) -> None:
        """Create an empty fake bucket."""
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.writes = 0
        self.conflict = False

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """Read retained content or raise the real SDK's missing-key shape."""
        if kwargs['Key'] not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        body, etag = self.objects[kwargs['Key']]
        return {'Body': io.BytesIO(body), 'ETag': etag}

    def put_object(self, **kwargs: Any) -> None:
        """Enforce conditional writes to exercise CAS retry handling."""
        previous = self.objects.get(kwargs['Key'])
        if self.conflict:
            self.conflict = False
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        assert (kwargs.get('IfMatch') == previous[1]) if previous else kwargs.get('IfNoneMatch') == '*'
        self.writes += 1
        self.objects[kwargs['Key']] = (kwargs['Body'], str(self.writes))


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> tuple[MemoryS3, list[bytes]]:
    """Replace AWS and HTTP with retained memory and captured exposition."""
    store = MemoryS3()
    pushes: list[bytes] = []
    monkeypatch.setenv('PLATFORM_DATA_BUCKET', 'existing-data')
    monkeypatch.setenv('PROMETHEUS_PUSHGATEWAY_URL', 'http://existing-pushgateway')
    monkeypatch.setattr(metrics.boto3, 'client', lambda service: store)
    monkeypatch.setattr(metrics, 'push_to_gateway', lambda url, **kwargs: pushes.append(generate_latest(kwargs['registry'])))
    return store, pushes


def test_completed_drift_survives_process_and_rejects_older_result(durable: tuple[MemoryS3, list[bytes]]) -> None:
    """S3 retains the completed snapshot, thresholds and timestamp after task exit."""
    store, pushes = durable
    registry = CollectorRegistry()
    Gauge('airbnb_dataset_drift', 'Drift', registry=registry).set(1)
    metrics.persist_registry('drift', registry, 100)
    registry = CollectorRegistry()
    Gauge('airbnb_dataset_drift', 'Drift', registry=registry).set(0)
    metrics.persist_registry('drift', registry, 99)
    recovered, _ = metrics._read(store, 'existing-data', metrics._key('drift'))
    assert recovered['completed_at'] == 100
    assert b'airbnb_dataset_drift 1.0' in generate_latest(metrics.registry_for(recovered))
    assert pushes[-1] == pushes[0]


def test_model_event_retry_is_idempotent_and_cas_conflict_retries(durable: tuple[MemoryS3, list[bytes]]) -> None:
    """A replayed audit event and a conditional-write retry do not overcount."""
    store, pushes = durable
    store.conflict = True
    metrics.publish_model_event('rollback', 'verified-event', timestamp=100)
    metrics.publish_model_event('rollback', 'verified-event', timestamp=100)
    assert b'airbnb_model_rollbacks_total 1.0' in pushes[-1]
    assert b'airbnb_model_last_rollback_timestamp_seconds 100.0' in pushes[-1]
    assert b'verified-event' not in pushes[-1]


def test_failed_pipeline_preserves_success_and_counts_once(durable: tuple[MemoryS3, list[bytes]]) -> None:
    """Failures retain successful freshness while recording a distinct terminal outcome."""
    _store, pushes = durable
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run = SimpleNamespace(dag_id='airbnb_drift_monitoring', run_id='success', start_date=now, end_date=now)
    metrics.pipeline_success({'dag_run': run})
    run.run_id = 'failed'
    metrics.pipeline_failure({'dag_run': run})
    metrics.pipeline_failure({'dag_run': run})
    operational = pushes[-2]
    assert b'airbnb_pipeline_last_success_timestamp_seconds{dag="airbnb_drift_monitoring"}' in operational
    assert b'airbnb_pipeline_failures_total{dag="airbnb_drift_monitoring"} 1.0' in operational
    assert b'airbnb_drift_evaluation_failures_total 1.0' in pushes[-1]


def test_state_is_committed_before_push_failure(durable: tuple[MemoryS3, list[bytes]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost gateway can be replayed from S3 without incrementing the event again."""
    store, _pushes = durable
    def fail(*args: Any, **kwargs: Any) -> None:
        """Model an unavailable gateway after durable commit."""
        raise OSError('gateway offline')
    monkeypatch.setattr(metrics, 'push_to_gateway', fail)
    with pytest.raises(OSError, match='gateway offline'):
        metrics.publish_model_event('promotion', 'a')
    state = json.loads(store.objects[metrics._key('operational')][0])
    assert state['counters']['airbnb_model_promotions_total'][0]['value'] == 1


@pytest.mark.parametrize('role', ['customer', 'run-123'])
def test_quality_labels_are_bounded(role: str) -> None:
    """Reject arbitrary caller labels before accessing any AWS service."""
    with pytest.raises(ValueError, match='role'):
        metrics.publish_offline_evaluation(role, {'mae': 1.0})


def test_delayed_old_callback_cannot_replace_newer_pipeline_result(durable: tuple[MemoryS3, list[bytes]]) -> None:
    """An old callback retry cannot erase a later completed run's health."""
    _store, pushes = durable
    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    metrics.pipeline_success({'dag_run': SimpleNamespace(dag_id='airbnb_model_training', run_id='newer', start_date=newer, end_date=newer)})
    metrics.pipeline_failure({'dag_run': SimpleNamespace(dag_id='airbnb_model_training', run_id='older', start_date=older, end_date=older)})
    assert b'airbnb_pipeline_last_run_success{dag="airbnb_model_training"} 1.0' in pushes[-1]


def test_replay_rejects_uncontracted_sensitive_labels() -> None:
    """A retained state cannot smuggle request IDs into Prometheus on replay."""
    with pytest.raises(ValueError, match='labels'):
        metrics.registry_for({'gauges': {'airbnb_dataset_drift': [{'labels': {'customer_id': 'private'}, 'value': 1}]}})
