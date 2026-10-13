# Drift monitoring and dashboards

## Run a drift check

The reference dataset is the validated training feature artifact. The production
dataset must contain the monitored feature columns and a `prediction` column.
By default, the last 10,000 production rows form the current window.

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

## Airflow and retraining events

`airbnb_drift_monitoring` runs daily at 03:00 UTC. Its paths, thresholds, model
version, and production-window size are environment-configurable. When the
drifted-column share reaches `DRIFT_RETRAINING_SHARE_THRESHOLD`, the branch emits
an atomic `airbnb.model.retraining.requested` JSON event under
`RETRAINING_EVENT_DIR`. Below threshold, the event task is skipped.

The event records the exact production path/window, model version, drift share,
and report path so a downstream retraining consumer can reproduce the trigger.

## Grafana

Import or provision the three dashboards in
`monitoring/grafana/dashboards/` using a Prometheus datasource with UID
`prometheus`:

- Infrastructure and Service Health;
- Model and Drift Health; and
- Airbnb Pricing Business Metrics.

The dashboards intentionally use only bounded labels. Request features and raw
listing data are never exported as metrics.
