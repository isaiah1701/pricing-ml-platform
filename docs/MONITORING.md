# Drift monitoring and dashboards

## Run a drift check

The reference dataset is the validated training feature artifact. The production
window is rebuilt from immutable prediction events under the private
`production/events/date=YYYY-MM-DD/hour=HH/` S3 prefix. Each event contains the
validated model features, prediction, concrete model version, prediction ID,
and timestamp, but no user identity or credentials. By default, the newest
10,000 events form the current window.

```sh
uv run python -m monitoring.drift_check \
  --reference-path data/listing_features.parquet \
  --production-path data/production_features.parquet \
  --production-window-rows 10000 \
  --model-version 17 \
  --output-dir monitoring/reports \
  --metrics-path monitoring/metrics/drift.prom
```

The job uses population stability index (PSI) consistently across numerical,
categorical, and prediction columns. A per-column score above `0.2` is drifted;
dataset drift defaults to at least 50% of monitored columns. Both thresholds and
the monitored column lists are configurable from the CLI.

Each successful run writes timestamped JSON and interactive HTML reports. The
JSON summary records the UTC timestamp, concrete model version, input paths,
window size, per-feature scores, prediction state, and aggregate state.
Airflow uploads both reports to `monitoring/drift/<timestamp>/` in the platform
bucket so they survive pod and cluster replacement.

## Prometheus integration

The batch job atomically writes standard Prometheus text exposition to
`monitoring/metrics/drift.prom`. Mount that directory into a Prometheus Node
Exporter configured with `--collector.textfile.directory`, or an equivalent
textfile collector. Scrape the collector alongside the serving application's
`GET /metrics` endpoint.

The batch metrics are:

- `airbnb_feature_drift_score{feature,kind,model_version}`;
- `airbnb_dataset_drift`;
- `airbnb_drifted_features`;
- `airbnb_prediction_drift`; and
- `airbnb_drift_last_success_timestamp_seconds`.

The drift job also exports per-feature missing ratios, unseen-category ratios,
valid-row coverage, invalid-row count, and production-source freshness.

## Delayed production model quality

Production MAE and related supervised metrics require actual outcomes; they
cannot be inferred from predictions or drift. Build a privacy-reviewed Parquet
or CSV file at `data/model_outcomes.parquet` with these columns:

- `prediction_id`, returned by `POST /predict`;
- `predicted_price` and the later `actual_price` using the same target definition;
- the concrete `model_version` returned by serving;
- a timezone-aware `outcome_timestamp`; and
- optional `room_type` and `neighbourhood_group` bounded segments.

Run the rolling check directly with:

```sh
uv run python -m monitoring.model_quality \
  --outcomes-path data/model_outcomes.parquet \
  --window-days 30 \
  --reference-mae-dollars 54.67 \
  --output-dir monitoring/reports \
  --metrics-path monitoring/metrics/model_quality.prom
```

Duplicate prediction IDs, non-positive actual prices, negative predictions,
missing fields, invalid timestamps, and future/out-of-window outcomes are not
used. Reports expose excluded-row and match-coverage counts. The scheduled
`airbnb_model_quality_monitoring` Airflow DAG runs daily at 04:00 UTC and
publishes:

- overall and segmented MAE, RMSE, median absolute error, signed mean bias, and R²;
- shares within $25 and within 20% of the actual outcome;
- matched sample counts, join coverage, and outcome freshness; and
- MAE degradation relative to the configured validated reference.

Compose shares both batch metric files with Node Exporter's textfile collector,
which Prometheus scrapes as the `node-exporter` job.

Install [airbnb-alerts.yml](../monitoring/prometheus/airbnb-alerts.yml) in
Prometheus and configure Alertmanager routing. Every alert links to a
version-controlled entry in [the runbook index](runbooks/README.md).
Production Alertmanager publishes firing and resolved alerts to the encrypted
SNS topic created by Terraform. Set `alert_notification_email` (or add another
SNS subscription) and confirm the AWS subscription before expecting delivery.

## Airflow and retraining events

`airbnb_drift_monitoring` runs daily at 03:00 UTC. Its paths, thresholds, model
version, and production-window size are environment-configurable. When the
drifted-column share reaches `DRIFT_RETRAINING_SHARE_THRESHOLD`, the branch emits
an atomic `airbnb.model.retraining.requested` JSON event under
`RETRAINING_EVENT_DIR`. Below threshold, the event task is skipped.

The event records the exact production path/window, model version, drift share,
and report path so a downstream retraining consumer can reproduce the trigger.

Serving exports `airbnb_prediction_events_total{outcome}`. Sustained S3 write
failures fire `AirbnbPredictionEventPersistenceFailing`; stale monitoring also
catches missing or broken daily compaction.

## Grafana

Import or provision the three dashboards in
`monitoring/grafana/dashboards/` using a Prometheus datasource with UID
`prometheus`:

- Infrastructure and Service Health;
- Model and Drift Health; and
- Airbnb Pricing Business Metrics.

The dashboards intentionally use only bounded labels. Request features and raw
listing data are never exported as metrics.

The serving ServiceMonitor preserves application labels so `/predict` remains
the request endpoint instead of the scrape port name. Serving dashboard queries
deduplicate the `job` and `service` scrape labels per pod: stable and canary
Services can point to the same pod between rollouts and must not double counts.

## Production web access

Production exposes Grafana at `https://grafana.platf0rm.site` and MLflow at
`https://mlflow.platf0rm.site`. Both routes terminate TLS at Traefik. Grafana
uses its built-in login, while the MLflow route requires Basic Auth before a
request reaches the otherwise internal MLflow service.

Retrieve the Grafana administrator username and password from its Kubernetes
Secret:

```sh
kubectl -n monitoring get secret kube-prometheus-stack-grafana \
  -o jsonpath='{.data.admin-user}' | base64 --decode
kubectl -n monitoring get secret kube-prometheus-stack-grafana \
  -o jsonpath='{.data.admin-password}' | base64 --decode
```

Retrieve the MLflow username and password from AWS Secrets Manager without
placing either value in Git:

```sh
aws secretsmanager get-secret-value \
  --region eu-west-2 \
  --secret-id prod-mlops-prod/mlflow-public-auth \
  --query SecretString \
  --output text | jq -r 'fromjson | .username, .password'
```

Prometheus and Alertmanager remain private because they do not have an
application login in this deployment.

## Reproducible dashboard validation

```sh
.venv/bin/pytest -q tests/test_monitoring_dashboards.py
.venv/bin/python scripts/validate_monitoring.py --rules-output /tmp/mlops-monitoring/dashboard-rules.yml
cp tests/monitoring/promql.yml /tmp/mlops-monitoring/promql.yml
promtool check rules /tmp/mlops-monitoring/dashboard-rules.yml monitoring/prometheus/airbnb-alerts.yml
promtool test rules /tmp/mlops-monitoring/promql.yml
```

Use Prometheus/promtool 3.5.0 (CI runs its existing official container). These
checks verify source/Helm copies, datasource IDs, ConfigMap provisioning labels,
ServiceMonitor `honorLabels`, application metric declarations, all dashboard
PromQL syntax and duplicate-scrape behavior using synthetic series. Two scrapes
of one pod count once; distinct pods still add up. Health traffic is excluded
from prediction request rate.

The infrastructure dashboard includes node CPU/memory from node exporter,
namespace-scoped inference container CPU/memory, and Airflow/MLflow deployment
and container readiness from kube-state-metrics. These Kubernetes panels are
expected to have no data in local Compose without those exporters. CPU and memory
use separate units/axes. Prediction latency includes queue wait and inference;
it excludes event persistence and final HTTP serialization, so it is not exactly
the end-to-end latency measured by k6.

Airflow DAG-failure metrics are **not currently exported**. Inspect failed runs in
Airflow; container readiness must not be interpreted as DAG success. MLflow's
replica readiness panel shows Kubernetes probe health, not an authenticated
registry read or artifact-download check. No new Airflow exporter or synthetic
production metric has been introduced.

Drift and model-quality panels consume batch-computed aggregate metrics from
the existing Airflow jobs/Pushgateway. Grafana does not calculate drift. Missing
drift or outcome series mean the batch inputs/jobs need investigation; do not
replace missing data with fake zero drift or fabricated outcomes.

Static validation is separate from live verification. For an approved deployed
release, port-forward the existing Prometheus service to localhost, evaluate the
dashboard queries through `/api/v1/query`, check that expected series are present
and recent, then inspect the three provisioned dashboards in Grafana. Empty query
results are not proof of healthy services. This refactor's Phase 4 validation was
offline; no production dashboard sync or live metric verification was performed.
