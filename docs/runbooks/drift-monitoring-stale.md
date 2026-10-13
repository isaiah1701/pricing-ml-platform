# AirbnbDriftMonitoringStale

## Impact

Production drift is unknown because no successful report has updated
`airbnb_drift_last_success_timestamp_seconds` for 26 hours. Predictions may
continue, but model-quality regressions can go undetected.

## Diagnosis

1. Check the latest `airbnb_drift_monitoring` Airflow run and task logs.
2. Confirm the reference and production paths exist and contain the configured columns.
3. Check Evidently dependency/import errors and write access to report and metrics paths.
4. Confirm the Prometheus textfile collector is scraping the generated `drift.prom` file.

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
