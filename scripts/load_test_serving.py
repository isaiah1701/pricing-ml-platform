"""Run a reproducible in-process load test against the prediction API."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from serving.app.api import create_app
from serving.app.config import Settings
from serving.app.model_loader import LoadedModel

LOGGER = logging.getLogger(__name__)
DEFAULT_REQUESTS = 2_000
DEFAULT_WARMUP_REQUESTS = 100
PAYLOAD: dict[str, object] = {
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


class BenchmarkModel:
    """Provide deterministic low-cost inference for transport benchmarking."""

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Return one constant valid price for each request row."""
        return np.full(len(features), 175.25)


def load_benchmark_model(settings: Settings) -> LoadedModel:
    """Return an in-memory versioned model without external dependencies."""
    return LoadedModel(
        model=BenchmarkModel(),
        version="load-test-v1",
        model_uri=f"memory:/{settings.model_name}/load-test-v1",
    )


def run_load_test(requests: int, warmup_requests: int) -> dict[str, float | int | str]:
    """Measure sequential prediction throughput and latency percentiles."""
    if requests < 1 or warmup_requests < 0:
        LOGGER.error("Load-test request counts must be non-negative and nonzero")
        raise ValueError("requests must be positive and warmup_requests non-negative")
    settings = Settings(
        MODEL_NAME="load-test-model",
        MODEL_ALIAS="champion",
        MODEL_REFRESH_INTERVAL_SECONDS=0,
        LOG_LEVEL="WARNING",
    )
    application = create_app(settings=settings, model_loader=load_benchmark_model)
    latencies_ms: list[float] = []
    with TestClient(application) as client:
        for _ in range(warmup_requests):
            response = client.post("/predict", json=PAYLOAD)
            response.raise_for_status()
        started = time.perf_counter()
        for _ in range(requests):
            request_started = time.perf_counter_ns()
            response = client.post("/predict", json=PAYLOAD)
            response.raise_for_status()
            elapsed_ns = time.perf_counter_ns() - request_started
            latencies_ms.append(elapsed_ns / 1_000_000.0)
        elapsed_seconds = time.perf_counter() - started
    percentiles = np.percentile(np.asarray(latencies_ms), [50, 95, 99])
    return {
        "mode": "single-client in-process ASGI",
        "requests": requests,
        "warmup_requests": warmup_requests,
        "request_rate_per_second": requests / elapsed_seconds,
        "p50_latency_ms": float(percentiles[0]),
        "p95_latency_ms": float(percentiles[1]),
        "p99_latency_ms": float(percentiles[2]),
    }


def _parse_args() -> argparse.Namespace:
    """Parse deterministic benchmark sizing options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument("--warmup-requests", type=int, default=DEFAULT_WARMUP_REQUESTS)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    """Run the benchmark and emit its machine-readable result."""
    logging.basicConfig(level=logging.WARNING)
    arguments = _parse_args()
    result = run_load_test(arguments.requests, arguments.warmup_requests)
    rendered = json.dumps(result, indent=2)
    if arguments.output is not None:
        arguments.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
