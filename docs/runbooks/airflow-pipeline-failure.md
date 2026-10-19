# Airflow pipeline failure

**Symptoms:** AirflowPipelineFailure; latest terminal run of a critical DAG failed.

**Dashboard:** Pipeline Health. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
airbnb_pipeline_last_run_success == 0
```

**Diagnostics:**

```sh
kubectl -n airflow get pods
kubectl -n airflow exec deployment/airflow -c scheduler -- airflow dags list-runs -d <dag_id>
```

**Recovery:** Inspect the failed task and dependency readiness in Airflow. Repair data/schema/permissions or runtime issues, then retry only the affected task/run. Check whether training checkpoints are compatible before resuming; do not start another full training run implicitly.

**Verification:** Require a successful terminal run and updated last-success timestamp. Confirm Feast materialisation/verification for the data DAG, and artifact evaluation gates for training.
