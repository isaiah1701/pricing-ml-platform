"""Serve one-listing predictions from the MLflow Registry champion."""

from __future__ import annotations

import asyncio
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
from serving.app.schemas import (
    HealthResponse,
    ListingFeatures,
    PredictionResponse,
    ReadinessResponse,
)

LOGGER = logging.getLogger(__name__)
DEMO_HTML_PATH = Path(__file__).with_name("demo.html")
SAFE_LOAD_ERROR = "Champion model is not ready"
SAFE_PREDICTION_ERROR = "Prediction failed"
SAFE_OVERLOADED_ERROR = "Prediction capacity is temporarily exhausted"
ModelLoader = Callable[[Settings], LoadedModel | PredictionModel]
ModelRefresher = Callable[[LoadedModel, Settings], LoadedModel]


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


def _create_metrics() -> ServingMetrics:
    """Create serving collectors in an application-local registry."""
    # Some imported frameworks configure Prometheus multiprocess mode during
    # test collection. Re-evaluate it when an app is actually created so this
    # process follows its current environment rather than import-time state.
    prometheus_values.ValueClass = prometheus_values.get_value_class()
    registry = CollectorRegistry()
    return ServingMetrics(
        registry=registry,
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
) -> FastAPI:
    """Create an instrumented application with safe periodic model refresh."""
    runtime_settings = settings or get_settings()
    metrics = _create_metrics()
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
                except ModelLoadError:
                    LOGGER.exception("Model refresh could not recover readiness")
                    continue
            else:
                try:
                    replacement = await asyncio.to_thread(
                        model_refresher, current, runtime_settings
                    )
                except Exception:
                    LOGGER.exception(
                        "Unexpected refresh failure; retaining model version %s",
                        current.version,
                    )
                    continue
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
        except ModelLoadError as exc:
            application.state.model_error = exc
            LOGGER.error("Application started without a ready champion model")
        record_loaded_model(application, application.state.loaded_model)
        if runtime_settings.model_refresh_interval_seconds > 0:
            refresh_task = asyncio.create_task(refresh_loop(application))
        yield
        if refresh_task is not None:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        LOGGER.info("Model-serving application stopped")

    application = FastAPI(
        title="Airbnb Price Predictor",
        version="1.0.0",
        description="Serve one NYC listing using an MLflow Registry alias.",
        lifespan=lifespan,
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
    async def predict(request: Request, listing: ListingFeatures) -> PredictionResponse:
        """Validate one listing and return a sanitized model prediction."""
        loaded: LoadedModel | None = getattr(request.app.state, "loaded_model", None)
        if loaded is None:
            raise HTTPException(status_code=503, detail=SAFE_LOAD_ERROR)
        started_at = time.perf_counter()
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    prediction_slots.acquire(),
                    timeout=runtime_settings.prediction_queue_timeout_seconds,
                )
                acquired = True
            except TimeoutError:
                metrics.prediction_rejected.inc()
                LOGGER.warning("Rejected prediction because execution capacity was full")
                raise HTTPException(
                    status_code=503,
                    detail=SAFE_OVERLOADED_ERROR,
                    headers={"Retry-After": "1"},
                ) from None
            metrics.prediction_inflight.inc()
            frame = listing.to_model_frame()
            raw_prediction = await asyncio.to_thread(loaded.predict, frame)
            values = np.asarray(raw_prediction).reshape(-1)
            if len(values) != 1:
                raise ValueError("model did not return exactly one value")
            predicted_price = float(values[0])
            if not np.isfinite(predicted_price) or predicted_price < 0.0:
                raise ValueError("model returned an invalid price")
        except HTTPException:
            raise
        except Exception:
            metrics.prediction_failures.inc()
            LOGGER.exception("Champion prediction failed")
            raise HTTPException(status_code=500, detail=SAFE_PREDICTION_ERROR) from None
        finally:
            if acquired:
                metrics.prediction_inflight.dec()
                prediction_slots.release()
            metrics.prediction_latency.observe(time.perf_counter() - started_at)
        metrics.predicted_price.observe(predicted_price)
        prediction_id = str(uuid4())
        prediction_timestamp = datetime.now(timezone.utc).isoformat()
        LOGGER.debug(
            "Completed prediction %s with model version %s",
            prediction_id,
            loaded.version,
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
