# Drift monitoring stale

**Symptoms:** DriftMonitoringStale; no successful evaluation in 26 hours, or no timestamp.

**Dashboard:** Model Quality & Drift / Pipeline Health. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
time() - airbnb_drift_last_success_timestamp_seconds
```

**Diagnostics:**

```sh
kubectl -n airflow exec deployment/airflow -c scheduler -- airflow dags list-runs -d airbnb_drift_monitoring
kubectl -n monitoring get pods,svc | rg pushgateway
```

**Recovery:** Check DAG pause/scheduler state, production-event availability, S3 permissions, reference features and metric publication errors. Reuse the bounded production window. Recover retained metric snapshots using the drift-alerting guide; replay does not make an old evaluation fresh. Resume one approved evaluation instead of inventing zero drift.

**Verification:** Require completed S3 reports plus a fresh success timestamp, scrape success, and no stale alert. Keep no-data distinct from healthy.
