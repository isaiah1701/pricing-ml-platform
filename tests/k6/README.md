# Reproducible inference load tests

Run with k6 1.3.0. All scenarios default to `http://127.0.0.1:8000` and check
readiness first. The three deterministic payloads reuse existing public
AB_NYC_2019 listing samples; no random synthetic features are generated.

```sh
k6 run tests/k6/smoke.js                         # 1 user, 10 seconds
k6 run tests/k6/baseline.js                      # 100 users, 5 minutes
k6 run tests/k6/burst.js                         # 5 → 100 → 5 users, 130 seconds
VUS=20 DURATION=30s k6 run tests/k6/baseline.js
STAGE_SECONDS=1 VUS=10 k6 run tests/k6/burst.js    # shortened 13-second script check
P95_MS=2000 MAX_ERROR_RATE=0.02 MIN_RPS=5 k6 run tests/k6/baseline.js
```

These are closed-loop virtual users: each completes a request, checks the result
and pauses one second. 100 users does not imply 100 requests per second.
`BASE_URL` selects the local/test service. Remote hosts require `ALLOW_REMOTE=true`
in addition to explicit operator approval. No CI job runs remote or heavy tests.
Do not run a production burst simply to populate dashboards.

Record an export for comparisons:

```sh
k6 run --summary-export /tmp/k6-summary.json tests/k6/baseline.js
k6 run --out json=/tmp/k6-burst-samples.json tests/k6/burst.js
```

- `prediction_errors`: fraction of requests with HTTP or response-contract errors.
- `prediction_latency`: end-to-end HTTP p95 in milliseconds, including failures.
- `predictions`: completed request count/rate (throughput, including failed responses).
- `recovery_latency` and `recovery_errors`: p95/error rate during the final
  60 seconds at five users, after the burst ramps down. A recovery sample count
  threshold ensures that period actually ran. Compare these with load-period
  samples in the JSON output; this measures the recovery window, not an exact
  recovery-time estimate. Killed/aborted runs cannot establish recovery.

Defaults are **provisional test thresholds**, not production SLOs: p95 <1000 ms,
errors <5%, checks >95%, at least one prediction. `MIN_RPS` defaults to zero
because the expected throughput depends on hardware, model and queue settings.
The current single-node deployment previously exceeded its latency target at
100 users; do not treat passing fixture tests as evidence of production capacity.

## Isolated CI/local fixture

To check the scripts and real FastAPI transport without MLflow, Redis or training:

```sh
.venv/bin/uvicorn fixture_app:app --app-dir tests/k6 --host 127.0.0.1 --port 8000
# In another terminal:
k6 run tests/k6/smoke.js
```

The fixture returns a fixed price and model version `local-fixture`. It exercises
the actual request validation, queue and HTTP response path. It does **not**
measure CatBoost inference, registry restoration or production performance.
`.github/workflows/k6.yml` runs only this one-user local smoke. Baseline and burst
must be run manually against an approved local/test environment.

The earlier `load-tests/` files remain preserved; use this directory for the
expanded reproducible suite and CI integration.
