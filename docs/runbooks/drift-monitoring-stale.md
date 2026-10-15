# AirbnbDriftMonitoringStale

## Impact

Production drift is unknown because no successful report has updated
`airbnb_drift_last_success_timestamp_seconds` for 26 hours. Predictions may
continue, but model-quality regressions can go undetected.

## Diagnosis

1. Check the latest `airbnb_drift_monitoring` Airflow run and task logs.
2. Verify new objects exist under `production/events/date=.../hour=.../` and
   inspect `airbnb_prediction_events_total{outcome="failed"}`.
3. Confirm the reference and compacted production paths contain the configured columns.
4. Check Evidently dependency/import errors and S3/report write permissions.
5. Confirm Pushgateway is exposing the generated drift metrics.

## Mitigation

Restore dataset access or worker capacity, rerun the failed DAG, and verify the
last-success timestamp advances. Treat reports from incomplete production
windows as invalid.

## Rollback

If a monitoring-code release caused the failure, redeploy the previous Airflow
image/DAG revision. Do not roll back or promote the serving model solely because
monitoring is stale.

## Ownership

ML Platform on-call owns response. Data Engineering owns production-window
delivery; SRE owns Airflow and Prometheus availability.
