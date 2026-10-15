"""Configure privacy-conscious OpenTelemetry export to MLflow tracing."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from serving.app.config import Settings

LOGGER = logging.getLogger(__name__)
INSTRUMENTATION_NAME = "prod-mlops-platform.serving"


def configure_tracing(
    application: FastAPI,
    settings: Settings,
    *,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Instrument one FastAPI app and export its spans to MLflow over OTLP/HTTP."""
    if not settings.tracing_enabled:
        application.state.tracer = trace.get_tracer(INSTRUMENTATION_NAME)
        LOGGER.info("OpenTelemetry tracing is disabled")
        return None

    endpoint = f"{settings.mlflow_tracking_uri.rstrip('/')}/v1/traces"
    span_exporter = exporter or OTLPSpanExporter(
        endpoint=endpoint,
        headers={"x-mlflow-experiment-id": settings.tracing_experiment_id},
    )
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.namespace": "prod-mlops",
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    application.state.tracer = provider.get_tracer(INSTRUMENTATION_NAME)
    FastAPIInstrumentor.instrument_app(
        application,
        tracer_provider=provider,
        excluded_urls="health,ready,metrics",
        exclude_spans=["receive", "send"],
    )
    LOGGER.info(
        "OpenTelemetry tracing enabled for service %s and MLflow experiment %s",
        settings.otel_service_name,
        settings.tracing_experiment_id,
    )
    return provider


def get_application_tracer(application: FastAPI) -> Tracer:
    """Return the application tracer configured during app construction."""
    tracer: Tracer = application.state.tracer
    return tracer
