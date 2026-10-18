# Operational runbooks

These procedures describe authorized recovery actions; this implementation did
not run live failure injection, deploy fixes or test email delivery.

| Symptom | Runbook |
| --- | --- |
| Inference unavailable | [Inference unavailable](inference-unavailable.md) |
| High inference error rate | [High inference error rate](serving-error-rate.md) |
| High inference latency | [High inference latency](prediction-latency.md) |
| Drift detected | [Drift detected](feature-drift.md) |
| Drift monitoring stale | [Drift monitoring stale](drift-monitoring-stale.md) |
| Airflow pipeline failure | [Airflow pipeline failure](airflow-pipeline-failure.md) |
| Model promotion failure | [Model promotion failure](model-promotion-failure.md) |
| Model rollback failure | [Model rollback failure](model-rollback-failure.md) |
| Canary rollout failure | [Canary rollout failure](canary-rollout-failure.md) |
| MLflow registry recovery | [MLflow registry recovery](mlflow-registry-recovery.md) |
| Grafana or Prometheus unavailable | [Grafana or Prometheus unavailable](monitoring-unavailable.md) |

See [drift alerting](../observability/drift-alerting.md) for notification routing
and planned teardown silencing, and [rollback](../observability/rollback.md) for
serialized promotion receipts and the Git revert procedure.
