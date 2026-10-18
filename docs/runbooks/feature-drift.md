# Drift detected

**Symptoms:** ModelDriftDetected; latest completed report exceeds the configured dataset share threshold.

**Dashboard:** Model Quality & Drift. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
airbnb_drift_share >= airbnb_drift_threshold
```

**Diagnostics:**

```sh
kubectl -n airflow get pods
kubectl -n airflow exec deployment/airflow -c scheduler -- airflow dags list-runs -d airbnb_drift_monitoring
```

**Recovery:** Review the exact completed report under s3://<platform-bucket>/monitoring/drift/<timestamp>/. Compare data quality, model changes and collection windows. Preserve the existing retraining request; a human must approve compute and model promotion. Do not switch aliases solely because drift occurred.

**Verification:** Confirm a new successful evaluation, fresh success timestamp and appropriate breach/resolution state. An unchanged report must not create a new notification identity.
