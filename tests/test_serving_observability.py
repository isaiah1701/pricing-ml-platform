"""Exercise registry failure visibility without sacrificing the loaded serving model."""
from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from serving.app.api import create_app
from serving.app.config import Settings
from serving.app.model_loader import LoadedModel, ModelLoadError
from tests.test_serving import FakePriceModel

LOGGER = logging.getLogger(__name__)


def _samples(client: TestClient, name: str) -> list[tuple[dict[str, str], float]]:
    """Extract one real exposition series family from the HTTP scrape endpoint."""
    return [
        (sample.labels, float(sample.value))
        for family in text_string_to_metric_families(client.get('/metrics').text)
        for sample in family.samples if sample.name == name
    ]


def test_failed_refresh_exposes_target_and_keeps_previous_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count a corrupt target and preserve readiness, identity and last good timestamp."""
    monkeypatch.delenv('PROMETHEUS_MULTIPROC_DIR', raising=False)
    monkeypatch.delenv('prometheus_multiproc_dir', raising=False)
    settings = Settings(MODEL_REFRESH_INTERVAL_SECONDS=0.02, LOG_LEVEL='WARNING')
    loaded = LoadedModel(FakePriceModel(), '1', 'models:/prices/1')

    def load(config: Settings) -> LoadedModel:
        """Load the initial good model without contacting MLflow."""
        return loaded

    def fail(current: LoadedModel, config: Settings) -> LoadedModel:
        """Model a changed champion whose artifact fails integrity verification."""
        assert current is loaded
        raise ModelLoadError('corrupt artifact', target_version='2', registry_available=True)

    with TestClient(create_app(settings=settings, model_loader=load, model_refresher=fail)) as client:
        initial = _samples(client, 'airbnb_model_refresh_last_success_timestamp_seconds')[0][1]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if _samples(client, 'airbnb_model_refresh_failures_total')[0][1] > 0:
                break
            time.sleep(0.005)
        assert _samples(client, 'airbnb_model_refresh_failures_total')[0][1] >= 1
        assert client.get('/ready').json()['version'] == '1'
        assert _samples(client, 'airbnb_readiness') == [({}, 1.0)]
        assert _samples(client, 'airbnb_registry_available') == [({}, 1.0)]
        assert _samples(client, 'airbnb_loaded_model_info')[0][0]['version'] == '1'
        assert _samples(client, 'airbnb_champion_model_info')[0][0]['version'] == '2'
        assert _samples(client, 'airbnb_model_refresh_last_success_timestamp_seconds')[0][1] == initial


def test_successful_refresh_replaces_info_without_accumulating_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update concrete identities atomically and keep only one model-version gauge."""
    monkeypatch.delenv('PROMETHEUS_MULTIPROC_DIR', raising=False)
    monkeypatch.delenv('prometheus_multiproc_dir', raising=False)
    settings = Settings(MODEL_REFRESH_INTERVAL_SECONDS=0.02, LOG_LEVEL='WARNING')
    first = LoadedModel(FakePriceModel(), '1', 'models:/prices/1')
    second = LoadedModel(FakePriceModel(), '2', 'models:/prices/2')

    def load(config: Settings) -> LoadedModel:
        """Expose the initial model."""
        return first

    def refresh(current: LoadedModel, config: Settings) -> LoadedModel:
        """Expose the replacement and then retain that same ready reference."""
        return second

    with TestClient(create_app(settings=settings, model_loader=load, model_refresher=refresh)) as client:
        deadline = time.monotonic() + 2
        while client.get('/ready').json()['version'] != '2' and time.monotonic() < deadline:
            time.sleep(0.005)
        for name in ('airbnb_loaded_model_info', 'airbnb_champion_model_info'):
            samples = _samples(client, name)
            assert len(samples) == 1
            assert samples[0][0]['version'] == '2'
        assert _samples(client, 'airbnb_model_refresh_failures_total') == [({}, 0.0)]


def test_unresolved_startup_does_not_publish_a_fictitious_champion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose not-ready and a failure while keeping unknown alias identity absent."""
    monkeypatch.delenv('PROMETHEUS_MULTIPROC_DIR', raising=False)
    monkeypatch.delenv('prometheus_multiproc_dir', raising=False)

    def fail(config: Settings) -> LoadedModel:
        """Simulate unavailable MLflow alias resolution."""
        raise ModelLoadError('registry unavailable', registry_available=False)

    with TestClient(create_app(settings=Settings(MODEL_REFRESH_INTERVAL_SECONDS=0), model_loader=fail)) as client:
        assert client.get('/ready').status_code == 503
        assert _samples(client, 'airbnb_readiness') == [({}, 0.0)]
        assert _samples(client, 'airbnb_registry_available') == [({}, 0.0)]
        assert _samples(client, 'airbnb_model_refresh_failures_total') == [({}, 1.0)]
        assert not _samples(client, 'airbnb_champion_model_info')
        assert _samples(client, 'airbnb_model_refresh_last_success_timestamp_seconds') == [({}, 0.0)]
