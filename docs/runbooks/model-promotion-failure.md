# Model promotion failure

**Symptoms:** Approved challenger failed provenance, alias transition or serving verification.

**Dashboard:** Pipeline Health / Model Quality & Drift. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
airbnb_model_promotions_total
```

**Diagnostics:**

```sh
kubectl -n airflow exec deployment/airflow -c scheduler -- ls /opt/airflow/training-checkpoints/promotions
kubectl -n airbnb-inference get pods,rollout
```

**Recovery:** Inspect the durable promotion receipt and MLflow approval/provenance tags. Do not delete the active lease or force an unrelated promotion. Retry the same operation after fixing transient faults. A failed serving check must enter verified rollback; follow the rollback guide. If another writer changed champion, stop for operator review.

**Verification:** Verify the receipt reaches a terminal state and serving reports either the validated new version or the confirmed prior version. Alias metadata alone is insufficient.
