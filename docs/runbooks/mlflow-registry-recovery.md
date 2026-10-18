# MLflow registry recovery

**Symptoms:** Registry metadata/alias missing after database or cluster recreation.

**Dashboard:** Pipeline Health / Model Inference. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
airbnb_champion_model_info
```

**Diagnostics:**

```sh
kubectl -n mlflow get pods,svc
kubectl -n airflow exec deployment/airflow -c scheduler -- python -m training.recovery verify
```

**Recovery:** Follow ../model-recovery.md: verify the retained S3 bundle first, restore into the reachable registry, then recover features and reconcile inference. Existing production champion still needs a one-time export before teardown. Preserve Object Lock and artifact bucket/state. Infrastructure recovery requires separate reviewed Terraform/Bootstrap execution.

**Verification:** Verify restored provenance/checksums, approved champion, /ready and prediction at the restored concrete version without training.
