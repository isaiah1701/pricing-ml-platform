# Model rollback failure

**Symptoms:** ModelRefreshFailure or rollback verification failed; prior champion is not confirmed in serving.

**Dashboard:** Model Inference / Model Quality & Drift. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
airbnb_model_rollback_failures_total
```

**Diagnostics:**

```sh
kubectl -n airbnb-inference get pods,rollout
kubectl -n airflow exec deployment/airflow -c scheduler -- ls /opt/airflow/training-checkpoints/promotions
```

**Recovery:** Inspect receipt, current champion/rollback aliases and artifact integrity. Follow the serialized retry procedure in ../observability/rollback.md. Do not overwrite a newer independent promotion. Restore dependency access, retry the same rollback and retain the lease until serving verification succeeds. Escalate blocked or corrupt artifacts to the ML platform owner.

**Verification:** Check /ready and predictions at the previous concrete version on ready pods, acceptable smoke latency, completed rollback receipt and success timestamp.
