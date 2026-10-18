"""Serve one-listing predictions from the MLflow Registry champion."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Span, Status, StatusCode
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import values as prometheus_values
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from serving.app.config import Settings, get_settings
from serving.app.model_loader import (
    LoadedModel,
    ModelLoadError,
    PredictionModel,
    load_champion_model,
    refresh_champion_model,
)
from serving.app.prediction_events import (
    PredictionEventRecorder,
    create_prediction_event_recorder,
)
from serving.app.schemas import (
    MODEL_FEATURE_COLUMNS,
    HealthResponse,
    ListingFeatures,
    PredictionResponse,
    ReadinessResponse,
)
from serving.app.tracing import configure_tracing, get_application_tracer

LOGGER = logging.getLogger(__name__)
DEMO_HTML_PATH = Path(__file__).with_name("demo.html")
SAFE_LOAD_ERROR = "Champion model is not ready"
SAFE_PREDICTION_ERROR = "Prediction failed"
SAFE_OVERLOADED_ERROR = "Prediction capacity is temporarily exhausted"
ModelLoader = Callable[[Settings], LoadedModel | PredictionModel]
ModelRefresher = Callable[[LoadedModel, Settings], LoadedModel]


def _safe_trace_inputs(listing: ListingFeatures) -> dict[str, object]:
    """Return bounded diagnostic inputs without exact coordinates or free text."""
    return {
        "neighbourhood_group": listing.neighbourhood_group,
        "room_type": listing.room_type,
        "minimum_nights_band": listing.minimum_nights_band,
        "availability_band": listing.availability_band,
        "location_cell": listing.location_cell,
        "has_reviews": listing.has_reviews,
        "feature_count": len(MODEL_FEATURE_COLUMNS),
    }


def _set_span_json(span: Span, key: str, value: dict[str, object]) -> None:
    """Set one deterministic JSON span attribute for MLflow rendering."""
    span.set_attribute(key, json.dumps(value, sort_keys=True, separators=(",", ":")))


def _trace_id(span: Span) -> str | None:
    """Return the active W3C trace identifier when the span is recording."""
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


@dataclass(frozen=True)
class ServingMetrics:
    """Own the isolated Prometheus collectors for one application instance."""

    registry: CollectorRegistry
    requests: Counter
    prediction_latency: Histogram
    prediction_failures: Counter
    readiness: Gauge
    model_info: Gauge
    predicted_price: Histogram
    prediction_inflight: Gauge
    prediction_rejected: Counter
    prediction_events: Counter
    refresh_failures: Counter
    refresh_last_success: Gauge
    champion_info: Gauge
    registry_available: Gauge


def _create_metrics() -> ServingMetrics:
    """Create serving collectors in an application-local registry."""
    # Some imported frameworks configure Prometheus multiprocess mode during
    # test collection. Re-evaluate it when an app is actually created so this
    # process follows its current environment rather than import-time state.
    prometheus_values.ValueClass = prometheus_values.get_value_class()
    registry = CollectorRegistry()
    return ServingMetrics(
        registry=registry,
        registry_available=Gauge(
            "airbnb_registry_available",
            "Whether the latest registry alias lookup succeeded; artifact failures are separate.",
            registry=registry,
        ),
        refresh_failures=Counter(
            "airbnb_model_refresh_failures_total",
            "Failed model alias resolution or preparation attempts.", registry=registry,
        ),
        refresh_last_success=Gauge(
            "airbnb_model_refresh_last_success_timestamp_seconds",
            "Last successful model alias resolution and load check.", registry=registry,
        ),
        champion_info=Gauge(
            "airbnb_champion_model_info",
            "Most recently resolved registry alias target, including failed loads.",
            ("model", "alias", "version"), registry=registry,
        ),
        requests=Counter(
            "airbnb_http_requests_total",
            "HTTP requests by endpoint and status.",
            ("endpoint", "method", "status"),
            registry=registry,
        ),
        prediction_latency=Histogram(
            "airbnb_prediction_latency_seconds",
            "Model prediction latency in seconds.",
            registry=registry,
        ),
        prediction_failures=Counter(
            "airbnb_prediction_failures_total",
            "Prediction failures after request validation.",
            registry=registry,
        ),
        readiness=Gauge(
            "airbnb_readiness",
            "Whether a model is loaded and ready for prediction.",
            registry=registry,
        ),
        model_info=Gauge(
            "airbnb_loaded_model_info",
            "Currently loaded MLflow model version.",
            ("model", "alias", "version"),
            registry=registry,
        ),
        predicted_price=Histogram(
            "airbnb_predicted_price_dollars",
            "Distribution of successful predicted nightly prices.",
            buckets=(25, 50, 75, 100, 150, 200, 300, 500, 1000, 2500, 5000, 10000),
            registry=registry,
        ),
        prediction_inflight=Gauge(
            "airbnb_prediction_inflight",
            "Predictions currently executing inside this process.",
            registry=registry,
        ),
        prediction_rejected=Counter(
            "airbnb_prediction_rejected_total",
            "Predictions rejected after the bounded execution queue filled.",
            registry=registry,
        ),
        prediction_events=Counter(
            "airbnb_prediction_events_total",
            "Prediction event persistence attempts by outcome.",
            ("outcome",),
            registry=registry,
        ),
    )


def _as_loaded_model(model: LoadedModel | PredictionModel, settings: Settings) -> LoadedModel:
    """Adapt legacy injected model doubles while preserving production metadata."""
    if isinstance(model, LoadedModel):
        return model
    return LoadedModel(
        model=model,
        version="unknown",
        model_uri=f"models:/{settings.model_name}@{settings.model_alias}",
    )


def create_app(
    *,
    settings: Settings | None = None,
    model_loader: ModelLoader = load_champion_model,
    model_refresher: ModelRefresher = refresh_champion_model,
    trace_exporter: SpanExporter | None = None,
    event_recorder: PredictionEventRecorder | None = None,
) -> FastAPI:
    """Create an instrumented application with safe periodic model refresh."""
    runtime_settings = settings or get_settings()
    metrics = _create_metrics()
    metrics.registry_available.set(float("nan"))
    runtime_event_recorder = (
        event_recorder
        if event_recorder is not None
        else create_prediction_event_recorder(runtime_settings)
    )
    prediction_slots = asyncio.Semaphore(
        runtime_settings.max_concurrent_predictions
    )

    def record_loaded_model(application: FastAPI, loaded: LoadedModel | None) -> None:
        """Publish readiness and model identity after an atomic state swap."""
        metrics.model_info.clear()
        if loaded is None:
            metrics.readiness.set(0)
            return
        metrics.readiness.set(1)
        metrics.model_info.labels(
            runtime_settings.model_name,
            runtime_settings.model_alias,
            loaded.version,
        ).set(1)

    def record_refresh_success(loaded: LoadedModel) -> None:
        """Replace alias identity and record a completed successful refresh check."""
        metrics.registry_available.set(1)
        metrics.champion_info.clear()
        metrics.champion_info.labels(
            runtime_settings.model_name, runtime_settings.model_alias, loaded.version
        ).set(1)
        metrics.refresh_last_success.set(time.time())

    def record_refresh_failure(error: Exception) -> None:
        """Count a failed check while keeping loaded-model readiness unchanged."""
        metrics.refresh_failures.inc()
        registry_available = getattr(error, "registry_available", None)
        if registry_available is not None:
            metrics.registry_available.set(float(registry_available))
        metrics.champion_info.clear()
        target = getattr(error, "target_version", None)
        if target:
            metrics.champion_info.labels(
                runtime_settings.model_name, runtime_settings.model_alias, target
            ).set(1)

    async def refresh_loop(application: FastAPI) -> None:
        """Periodically replace state only after a complete successful load."""
        interval = runtime_settings.model_refresh_interval_seconds
        while True:
            await asyncio.sleep(interval)
            current: LoadedModel | None = getattr(
                application.state, "loaded_model", None
            )
            if current is None:
                try:
                    replacement = _as_loaded_model(
                        await asyncio.to_thread(model_loader, runtime_settings),
                        runtime_settings,
                    )
                except ModelLoadError as exc:
                    record_refresh_failure(exc)
                    LOGGER.exception("Model refresh could not recover readiness")
                    continue
            else:
                try:
                    replacement = await asyncio.to_thread(
                        model_refresher, current, runtime_settings
                    )
                except Exception as exc:
                    record_refresh_failure(exc)
                    LOGGER.exception(
                        "Model refresh failure; retaining model version %s",
                        current.version,
                    )
                    continue
            record_refresh_success(replacement)
            if replacement is not current:
                application.state.loaded_model = replacement
                application.state.model = replacement.model
                application.state.model_version = replacement.version
                record_loaded_model(application, replacement)
                LOGGER.info("Atomically activated model version %s", replacement.version)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Load the aliased model once and retain clean not-ready state on failure."""
        logging.basicConfig(
            level=runtime_settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        application.state.model = None
        application.state.loaded_model = None
        application.state.model_version = None
        application.state.model_error = None
        refresh_task: asyncio.Task[None] | None = None
        try:
            loaded = _as_loaded_model(model_loader(runtime_settings), runtime_settings)
            application.state.loaded_model = loaded
            application.state.model = loaded.model
            application.state.model_version = loaded.version
            record_refresh_success(loaded)
        except ModelLoadError as exc:
            record_refresh_failure(exc)
            application.state.model_error = exc
            LOGGER.error("Application started without a ready champion model")
        record_loaded_model(application, application.state.loaded_model)
        if runtime_settings.model_refresh_interval_seconds > 0:
            refresh_task = asyncio.create_task(refresh_loop(application))
        try:
            yield
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
            tracer_provider = getattr(application.state, "tracer_provider", None)
            if tracer_provider is not None:
                tracer_provider.shutdown()
            LOGGER.info("Model-serving application stopped")

    application = FastAPI(
        title="Airbnb Price Predictor",
        version="1.0.0",
        description="Serve one NYC listing using an MLflow Registry alias.",
        lifespan=lifespan,
    )
    application.state.tracer_provider = configure_tracing(
        application, runtime_settings, exporter=trace_exporter
    )

    @application.get("/demo", response_class=FileResponse, include_in_schema=False)
    async def demo() -> FileResponse:
        """Serve the constrained browser sandbox for manual predictions."""
        return FileResponse(DEMO_HTML_PATH, media_type="text/html")

    @application.middleware("http")
    async def observe_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Count bounded endpoint/status combinations for every HTTP response."""
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            endpoint = getattr(route, "path", "unmatched")
            metrics.requests.labels(endpoint, request.method, str(status)).inc()

    @application.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        """Expose the application-local Prometheus scrape payload."""
        return Response(
            content=generate_latest(metrics.registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Return a cheap liveness result without invoking dependencies."""
        return HealthResponse()

    @application.get("/ready", response_model=ReadinessResponse)
    async def ready(request: Request) -> ReadinessResponse:
        """Return readiness only after startup loaded the registry model."""
        loaded: LoadedModel | None = getattr(request.app.state, "loaded_model", None)
        if loaded is None:
            raise HTTPException(status_code=503, detail=SAFE_LOAD_ERROR)
        return ReadinessResponse(
            model=runtime_settings.model_name,
            alias=runtime_settings.model_alias,
            version=loaded.version,
        )

    @application.post("/predict", response_model=PredictionResponse)
    async def predict(
        request: Request,
        response: Response,
        listing: ListingFeatures,
    ) -> PredictionResponse:
        """Validate one listing and return a sanitized model prediction."""
        root_span = trace.get_current_span()
        root_span.set_attribute("mlflow.spanType", "CHAIN")
        root_span.set_attribute("app.operation", "predict")
        _set_span_json(root_span, "mlflow.spanInputs", _safe_trace_inputs(listing))
        loaded: LoadedModel | None = getattr(request.app.state, "loaded_model", None)
        if loaded is None:
            root_span.set_attribute("app.error.type", "model_not_ready")
            root_span.set_status(Status(StatusCode.ERROR, SAFE_LOAD_ERROR))
            raise HTTPException(status_code=503, detail=SAFE_LOAD_ERROR)
        started_at = time.perf_counter()
        acquired = False
        tracer = get_application_tracer(request.app)
        try:
            with tracer.start_as_current_span("prediction.queue") as queue_span:
                queue_started_at = time.perf_counter()
                queue_span.set_attribute(
                    "queue.timeout_seconds",
                    runtime_settings.prediction_queue_timeout_seconds,
                )
                try:
                    await asyncio.wait_for(
                        prediction_slots.acquire(),
                        timeout=runtime_settings.prediction_queue_timeout_seconds,
                    )
                    acquired = True
                    queue_span.set_attribute("queue.acquired", True)
                    queue_span.set_attribute(
                        "queue.wait_ms",
                        (time.perf_counter() - queue_started_at) * 1000,
                    )
                    queue_span.set_status(Status(StatusCode.OK))
                except TimeoutError:
                    queue_span.set_attribute("queue.acquired", False)
                    queue_span.set_status(
                        Status(StatusCode.ERROR, SAFE_OVERLOADED_ERROR)
                    )
                    metrics.prediction_rejected.inc()
                    LOGGER.warning(
                        "Rejected prediction because execution capacity was full"
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=SAFE_OVERLOADED_ERROR,
                        headers={"Retry-After": "1"},
                    ) from None
            metrics.prediction_inflight.inc()
            with tracer.start_as_current_span("features.transform") as feature_span:
                frame = listing.to_model_frame()
                feature_span.set_attribute("features.count", len(frame.columns))
                feature_span.set_attribute("features.rows", len(frame.index))
                feature_span.set_status(Status(StatusCode.OK))
            with tracer.start_as_current_span(
                "model.predict",
                attributes={
                    "mlflow.spanType": "MODEL",
                    "model.name": runtime_settings.model_name,
                    "model.alias": runtime_settings.model_alias,
                    "model.version": loaded.version,
                    "model.batch_size": 1,
                },
            ) as model_span:
                _set_span_json(
                    model_span,
                    "mlflow.spanInputs",
                    {"rows": 1, "features": len(frame.columns)},
                )
                raw_prediction = await asyncio.to_thread(loaded.predict, frame)
                values = np.asarray(raw_prediction).reshape(-1)
                if len(values) != 1:
                    raise ValueError("model did not return exactly one value")
                predicted_price = float(values[0])
                if not np.isfinite(predicted_price) or predicted_price < 0.0:
                    raise ValueError("model returned an invalid price")
                _set_span_json(
                    model_span,
                    "mlflow.spanOutputs",
                    {"predicted_price": round(predicted_price, 2)},
                )
                model_span.set_status(Status(StatusCode.OK))
        except HTTPException:
            root_span.set_attribute("app.error.type", "capacity_exhausted")
            root_span.set_status(Status(StatusCode.ERROR, SAFE_OVERLOADED_ERROR))
            raise
        except Exception:
            metrics.prediction_failures.inc()
            root_span.set_attribute("app.error.type", "prediction_failed")
            root_span.set_status(Status(StatusCode.ERROR, SAFE_PREDICTION_ERROR))
            LOGGER.exception("Champion prediction failed")
            raise HTTPException(status_code=500, detail=SAFE_PREDICTION_ERROR) from None
        finally:
            if acquired:
                metrics.prediction_inflight.dec()
                prediction_slots.release()
            metrics.prediction_latency.observe(time.perf_counter() - started_at)
        metrics.predicted_price.observe(predicted_price)
        prediction_id = str(uuid4())
        prediction_datetime = datetime.now(timezone.utc)
        prediction_timestamp = prediction_datetime.isoformat()
        trace_id = _trace_id(root_span)
        event_persisted = runtime_event_recorder is None
        if runtime_event_recorder is not None:
            with tracer.start_as_current_span("prediction.persist") as persist_span:
                persist_span.set_attribute("storage.system", "aws.s3")
                try:
                    event_key = await asyncio.to_thread(
                        runtime_event_recorder.record,
                        listing,
                        prediction_id=prediction_id,
                        prediction_timestamp=prediction_datetime,
                        predicted_price=predicted_price,
                        model_version=loaded.version,
                    )
                    persist_span.set_attribute("storage.key", event_key)
                    persist_span.set_status(Status(StatusCode.OK))
                    metrics.prediction_events.labels("persisted").inc()
                    event_persisted = True
                except Exception:
                    persist_span.set_status(
                        Status(StatusCode.ERROR, "Prediction event persistence failed")
                    )
                    metrics.prediction_events.labels("failed").inc()
                    LOGGER.exception(
                        "Prediction event persistence failed for %s", prediction_id
                    )
        _set_span_json(
            root_span,
            "mlflow.spanOutputs",
            {
                "model_version": loaded.version,
                "predicted_price": round(predicted_price, 2),
                "prediction_id": prediction_id,
                "monitoring_event_persisted": event_persisted,
            },
        )
        root_span.set_attribute("monitoring.event_persisted", event_persisted)
        root_span.set_attribute("app.prediction.id", prediction_id)
        root_span.set_status(Status(StatusCode.OK))
        if trace_id is not None:
            response.headers["X-Trace-Id"] = trace_id
        LOGGER.debug(
            "Completed prediction %s with model version %s and trace %s",
            prediction_id,
            loaded.version,
            trace_id or "disabled",
        )
        return PredictionResponse(
            prediction_id=prediction_id,
            prediction_timestamp=prediction_timestamp,
            predicted_price=predicted_price,
            model_name=runtime_settings.model_name,
            model_alias=runtime_settings.model_alias,
            model_version=loaded.version,
        )

    return application


app = create_app()
