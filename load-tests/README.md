# Prediction load test

Requires [k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) and a running
API with a ready champion model. Run from the repository root:

```sh
k6 run load-tests/predict.js
```

The default target is the local Compose API (`http://localhost:8080`). To test
the deployed API, explicitly set its URL:

```sh
BASE_URL=https://platf0rm.site k6 run load-tests/predict.js
```

This runs **100 concurrent virtual users for 5 minutes**. Each user sends one
`POST /predict`, checks the response, then waits 1 second. Expect up to roughly
100 requests/second, depending on response time; 100 users is not a fixed request
rate. A failed `/ready` check stops the run before the load starts.

`listings.json` contains three feature-only samples from the repository's public
`data/listing_features.parquet`, one per supported room type. Users cycle through
them. This is a small repeatable workload, not a representative production mix.

The test fails if prediction HTTP failures reach 5%, fewer than 95% of checks
pass, or prediction HTTP p95 reaches 250 ms. The error and latency limits follow
the existing serving alert limits. k6 measures full HTTP time, including network
and persistence, whereas the server latency histogram measures prediction work.
This five-minute run does not satisfy the alerts' ten-minute firing duration.

For a quick connectivity check before the full run:

```sh
BASE_URL=http://localhost:8080 k6 run --vus 1 --duration 10s load-tests/predict.js
```

With Prometheus scraping the API, the existing **Infrastructure and Service
Health** and **Airbnb Pricing Business Metrics** dashboards will show prediction
volume, latency, errors, success rate, and predicted prices. Use a recent time
range and allow at least two scrapes. No k6 output exporter is required for these
application metrics. A service port-forward exercises the selected pod; use the
ingress URL to exercise request routing across replicas.

Successful predictions can also populate the S3 event window when event
persistence is configured. **Model and Drift Health** still needs the drift DAG
to run; this narrow synthetic mix may report drift. Production MAE/RMSE and
quality panels require real matched outcomes and the quality job; load traffic
alone cannot populate them. See [monitoring](../docs/MONITORING.md).
