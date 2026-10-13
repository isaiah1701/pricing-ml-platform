# Model registry and serving

## What is served

The training job logs a cloudpickle-serialized `CatBoostPriceEnsembleRegressor`
with MLflow's sklearn flavor under the logged-model name `model`. The fitted
artifact contains feature selection, categorical handling, the three CatBoost
members, the `log1p` target transformation, and inverse transformation. FastAPI
does not recreate any of those operations.

The current MLflow 3 run stores `model` as a logged-model object rather than a
classic run artifact. Therefore `scripts/register_model.py` resolves the ready
logged model owned by `CHAMPION_RUN_ID` and registers its model URI. This is the
working equivalent of `runs:/<run-id>/model` for this repository's actual
artifact layout.

The fitted estimator consumes these required, non-null columns in this exact
order:

1. `neighbourhood_group`
2. `neighbourhood`
3. `latitude`
4. `longitude`
5. `room_type`
6. `minimum_nights`
7. `number_of_reviews`
8. `reviews_per_month`
9. `calculated_host_listings_count`
10. `availability_365`
11. `has_reviews`
12. `days_since_last_review`
13. `minimum_nights_band`
14. `availability_band`
15. `location_cell`
16. `distance_to_midtown_km`

These are engineered model inputs, not the original CSV schema. Feast has 18
features, but the fitted estimator deliberately excludes `availability_ratio`
and `neighbourhood_room_type`. The historical MLflow signature was inferred
before that internal selection and still lists all 18; the API follows the
fitted estimator's authoritative `feature_names_in_` contract.

Feast and Redis are not queried during inference. The endpoint accepts the
complete feature vector, so an online-store lookup would add a dependency
without supplying missing information. A later entity-ID API can introduce a
separate Feast retrieval layer if that becomes a product requirement.

## Local workflow

From the repository root, start the same local MLflow backend and artifact
directory used by the tracked runs:

```sh
uv run mlflow server \
  --backend-store-uri sqlite:///$(pwd)/mlflow.db \
  --default-artifact-root $(pwd)/mlruns \
  --host 0.0.0.0 \
  --allowed-hosts localhost,127.0.0.1,host.docker.internal \
  --port 5000
```

Select a finished run in <http://localhost:5000>, then register it and move the
alias atomically:

```sh
export CHAMPION_RUN_ID=<32-character-run-id>
export MLFLOW_TRACKING_URI=http://localhost:5000
export MODEL_NAME=airbnb-price-predictor
export MODEL_ALIAS=champion
uv run python -m scripts.register_model
```

The script is idempotent for the same source run/logged model. It copies only
metadata present on the run, including overall and ordinary-market metrics,
model family, and Git SHA when MLflow recorded one.

Start the API:

```sh
export MLFLOW_TRACKING_URI=http://localhost:5000
export MODEL_NAME=airbnb-price-predictor
export MODEL_ALIAS=champion
export MODEL_REFRESH_INTERVAL_SECONDS=60
uv run uvicorn serving.app.api:app --host 0.0.0.0 --port 8000
```

Probe and predict:

```sh
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/ready
curl --fail http://localhost:8000/metrics
curl --fail-with-body -X POST http://localhost:8000/predict \
  -H 'Content-Type: application/json' \
  -d '{
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
    "distance_to_midtown_km": 0.0
  }'
```

Run unit tests without MLflow, Redis, or AWS:

```sh
uv sync --extra test
uv run pytest
```

An optional real-registry smoke test is:

```sh
MLFLOW_TRACKING_URI=http://localhost:5000 uv run python - <<'PY'
from serving.app.config import Settings
from serving.app.model_loader import build_model_uri, load_champion_model

settings = Settings()
model = load_champion_model(settings)
print(build_model_uri(settings), type(model).__name__)
PY
```

## Docker

Build the pinned, non-root runtime image:

```sh
docker build -f serving/app/Dockerfile -t airbnb-price-predictor:local .
```

Existing local MLflow artifacts contain absolute host paths. On Linux, mount
the repository at the same path so the container can read those existing
artifacts (new production S3 artifacts do not need this mount):

```sh
docker run --rm --name airbnb-api -p 8000:8000 \
  --add-host host.docker.internal:host-gateway \
  -e MLFLOW_TRACKING_URI=http://host.docker.internal:5000 \
  -e MODEL_NAME=airbnb-price-predictor \
  -e MODEL_ALIAS=champion \
  -v "$PWD:$PWD:ro" \
  airbnb-price-predictor:local
```

The process starts even when registry loading fails so `/health` remains a true
liveness probe; `/ready` and `/predict` return sanitized HTTP 503 responses.
At startup the loader resolves the alias to a concrete MLflow model version.
`/ready` and successful prediction responses report that version. Prediction
responses also contain an opaque `prediction_id` and UTC `prediction_timestamp`
so a downstream, access-controlled pipeline can join delayed actual outcomes
without placing request features in Prometheus labels. Every
`MODEL_REFRESH_INTERVAL_SECONDS` (60 seconds by default), the process checks the
alias. A changed version is completely loaded before a single state reference is
atomically swapped; failed checks or loads leave the current model serving.
Set the interval to `0` to disable refresh.

## Prometheus observability

`GET /metrics` exposes an application-local Prometheus registry with:

- `airbnb_http_requests_total` by bounded endpoint, method, and status;
- `airbnb_prediction_latency_seconds`;
- `airbnb_prediction_failures_total`;
- `airbnb_readiness`;
- `airbnb_loaded_model_info{model,alias,version}`; and
- `airbnb_predicted_price_dollars`.

No listing features or other request data are used as metric labels.

## Reproducible load test

The checked-in benchmark exercises request validation, FastAPI middleware,
in-memory prediction, response serialization, and Prometheus updates without
requiring MLflow or network I/O:

```sh
uv run python -m scripts.load_test_serving \
  --requests 2000 \
  --warmup-requests 100
```

Measured on 2026-09-29 under WSL2/Linux 5.15 with Python 3.14.0 and an Intel
Core i7-1355U, using one sequential in-process ASGI client:

| Requests/sec | p50 | p95 | p99 |
|---:|---:|---:|---:|
| 409.81 | 2.06 ms | 4.74 ms | 6.42 ms |

This is a repeatable serving-stack regression baseline, not a network capacity
claim. Production throughput should additionally be tested over HTTP with the
deployed CatBoost artifact and intended worker count.
