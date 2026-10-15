"""Focused API tests that require no live infrastructure."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from serving.app.api import create_app
from serving.app.config import Settings
from serving.app.model_loader import ModelLoadError, PredictionModel
from serving.app.schemas import MODEL_FEATURE_COLUMNS

LOGGER = logging.getLogger(__name__)


class FakePriceModel:
    """Capture model input and return a deterministic prediction."""

    def __init__(self, predicted_price: float = 175.25) -> None:
        """Store the prediction returned by the test double."""
        self.predicted_price = predicted_price
        self.received: pd.DataFrame | None = None

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Capture the exact frame supplied by the transport layer."""
        self.received = features.copy()
        return np.array([self.predicted_price])


class BlockingPriceModel(FakePriceModel):
    """Hold one prediction so the bounded queue can be exercised."""

    def __init__(self) -> None:
        """Create coordination events for a controlled slow prediction."""
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Block until the test releases the only prediction slot."""
        self.started.set()
        if not self.release.wait(timeout=5.0):
            raise TimeoutError("test prediction was not released")
        return super().predict(features)


@pytest.fixture
def listing_payload() -> dict[str, object]:
    """Return one request matching the fitted estimator contract."""
    return {
        "neighbourhood_group": "Manhattan",
        "neighbourhood": "Midtown",
        "latitude": 40.758,
        "longitude": -73.9855,
        "room_type": "Entire home/apt",
        "minimum_nights": 2,
        "number_of_reviews": 10,
        "reviews_per_month": 1.25,
        "calculated_host_listings_count": 1,
        "availability_365": 120,
        "has_reviews": 1,
        "days_since_last_review": 30,
        "minimum_nights_band": "short",
        "availability_band": "medium",
        "location_cell": "40.76_-73.99",
        "distance_to_midtown_km": 0.0,
    }


@pytest.fixture
def fake_model() -> FakePriceModel:
    """Return a deterministic in-memory prediction model."""
    return FakePriceModel()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return isolated runtime settings for tests."""
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    return Settings(
        MLFLOW_TRACKING_URI="http://mlflow.test:5000",
        MODEL_NAME="test-airbnb-model",
        MODEL_ALIAS="candidate",
        MODEL_REFRESH_INTERVAL_SECONDS=0,
        LOG_LEVEL="WARNING",
    )


def successful_loader(model: PredictionModel) -> Callable[[Settings], PredictionModel]:
    """Return a loader callback that exposes the supplied model."""

    def load(settings: Settings) -> PredictionModel:
        """Return the fixed model without accessing MLflow."""
        del settings
        return model

    return load


def test_health_does_not_execute_inference(
    fake_model: FakePriceModel, settings: Settings
) -> None:
    """Return process health without making a prediction."""
    app = create_app(settings=settings, model_loader=successful_loader(fake_model))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert fake_model.received is None


def test_demo_serves_constrained_prediction_form(
    fake_model: FakePriceModel, settings: Settings
) -> None:
    """Serve the browser sandbox without requiring a frontend build."""
    app = create_app(settings=settings, model_loader=successful_loader(fake_model))
    with TestClient(app) as client:
        response = client.get("/demo")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="neighbourhood"' in response.text
    assert 'fetch("/predict"' in response.text
    assert "Demo caveat" in response.text


def test_ready_identifies_loaded_alias(
    fake_model: FakePriceModel, settings: Settings
) -> None:
    """Report configured model identity after successful startup."""
    app = create_app(settings=settings, model_loader=successful_loader(fake_model))
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "model": "test-airbnb-model",
        "alias": "candidate",
        "version": "unknown",
    }


def test_startup_failure_leaves_service_not_ready(settings: Settings) -> None:
    """Keep liveness available while returning 503 when registry loading fails."""

    def unavailable_loader(settings: Settings) -> PredictionModel:
        """Simulate an unavailable registry alias during startup."""
        del settings
        raise ModelLoadError("registry unavailable")

    app = create_app(settings=settings, model_loader=unavailable_loader)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Champion model is not ready"}


def test_predict_validates_request_schema(
    fake_model: FakePriceModel,
    settings: Settings,
    listing_payload: dict[str, object],
) -> None:
    """Reject missing, invalid, and unexpected fitted features."""
    app = create_app(settings=settings, model_loader=successful_loader(fake_model))
    missing = {key: value for key, value in listing_payload.items() if key != "room_type"}
    invalid = {**listing_payload, "availability_365": 366}
    extra = {**listing_payload, "bedrooms": 2}
    with TestClient(app) as client:
        assert client.post("/predict", json=missing).status_code == 422
        assert client.post("/predict", json=invalid).status_code == 422
        assert client.post("/predict", json=extra).status_code == 422


def test_predict_preserves_order_and_response_schema(
    fake_model: FakePriceModel,
    settings: Settings,
    listing_payload: dict[str, object],
) -> None:
    """Preserve feature ordering and return model identity with the price."""
    app = create_app(settings=settings, model_loader=successful_loader(fake_model))
    with TestClient(app) as client:
        response = client.post("/predict", json=listing_payload)
    assert response.status_code == 200
    payload = response.json()
    assert payload["prediction_id"]
    assert payload["prediction_timestamp"].endswith("+00:00")
    assert {
        key: value
        for key, value in payload.items()
        if key not in {"prediction_id", "prediction_timestamp"}
    } == {
        "predicted_price": 175.25,
        "model_name": "test-airbnb-model",
        "model_alias": "candidate",
        "model_version": "unknown",
    }
    assert fake_model.received is not None
    assert tuple(fake_model.received.columns) == MODEL_FEATURE_COLUMNS
    assert fake_model.received.shape == (1, len(MODEL_FEATURE_COLUMNS))


def test_predict_rejects_when_bounded_execution_queue_is_full(
    listing_payload: dict[str, object],
) -> None:
    """Return a retryable response instead of oversubscribing model execution."""
    model = BlockingPriceModel()
    runtime_settings = Settings(
        MODEL_REFRESH_INTERVAL_SECONDS=0,
        MAX_CONCURRENT_PREDICTIONS=1,
        PREDICTION_QUEUE_TIMEOUT_SECONDS=0.05,
        LOG_LEVEL="WARNING",
    )
    app = create_app(
        settings=runtime_settings,
        model_loader=successful_loader(model),
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.post, "/predict", json=listing_payload)
        assert model.started.wait(timeout=2.0)
        rejected = client.post("/predict", json=listing_payload)
        model.release.set()
        successful = first.result(timeout=2.0)
        metrics = client.get("/metrics").text
    assert successful.status_code == 200
    assert rejected.status_code == 503
    assert rejected.headers["retry-after"] == "1"
    assert "airbnb_prediction_rejected_total 1.0" in metrics


def test_metrics_report_requests_predictions_and_model_version(
    fake_model: FakePriceModel,
    settings: Settings,
    listing_payload: dict[str, object],
) -> None:
    """Expose bounded request, latency, readiness, version, and price metrics."""
    app = create_app(settings=settings, model_loader=successful_loader(fake_model))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/predict", json=listing_payload).status_code == 200
        response = client.get("/metrics")
    assert response.status_code == 200
    payload = response.text
    assert 'airbnb_http_requests_total{endpoint="/health",method="GET",status="200"} 1.0' in payload
    assert 'airbnb_http_requests_total{endpoint="/predict",method="POST",status="200"} 1.0' in payload
    assert "airbnb_prediction_latency_seconds_count 1.0" in payload
    assert "airbnb_prediction_failures_total 0.0" in payload
    assert "airbnb_readiness 1.0" in payload
    assert 'airbnb_loaded_model_info{alias="candidate",model="test-airbnb-model",version="unknown"} 1.0' in payload
    assert "airbnb_predicted_price_dollars_count 1.0" in payload


def test_prediction_failure_is_counted(
    settings: Settings, listing_payload: dict[str, object]
) -> None:
    """Count sanitized inference failures without observing a predicted price."""
    failing_model = FakePriceModel(predicted_price=float("nan"))
    app = create_app(settings=settings, model_loader=successful_loader(failing_model))
    with TestClient(app) as client:
        assert client.post("/predict", json=listing_payload).status_code == 500
        metrics = client.get("/metrics").text
    assert "airbnb_prediction_failures_total 1.0" in metrics
    assert "airbnb_prediction_latency_seconds_count 1.0" in metrics
    assert "airbnb_predicted_price_dollars_count 0.0" in metrics


def test_predict_exports_http_and_model_spans_without_request_payload(
    fake_model: FakePriceModel,
    listing_payload: dict[str, object],
) -> None:
    """Export correlated request and inference spans without sensitive features."""
    exporter = InMemorySpanExporter()
    tracing_settings = Settings(
        MLFLOW_TRACKING_URI="http://mlflow.test:5000",
        MODEL_NAME="test-airbnb-model",
        MODEL_ALIAS="candidate",
        MODEL_REFRESH_INTERVAL_SECONDS=0,
        TRACING_ENABLED=True,
        MLFLOW_TRACING_EXPERIMENT_ID="1",
        OTEL_SERVICE_NAME="test-airbnb-inference",
        LOG_LEVEL="WARNING",
    )
    app = create_app(
        settings=tracing_settings,
        model_loader=successful_loader(fake_model),
        trace_exporter=exporter,
    )
    with TestClient(app) as client:
        response = client.post("/predict", json=listing_payload)

    assert response.status_code == 200
    spans = exporter.get_finished_spans()
    request_span = next(span for span in spans if span.name == "POST /predict")
    model_span = next(span for span in spans if span.name == "model.predict")
    assert model_span.parent is not None
    assert model_span.parent.span_id == request_span.context.span_id
    assert model_span.attributes == {
        "mlflow.spanType": "MODEL",
        "model.name": "test-airbnb-model",
        "model.alias": "candidate",
        "model.version": "unknown",
    }
    serialized_attributes = repr(
        [dict(span.attributes or {}) for span in spans]
    )
    assert "Midtown" not in serialized_attributes
    assert "40.758" not in serialized_attributes
