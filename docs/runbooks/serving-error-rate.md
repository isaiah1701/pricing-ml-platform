# High inference error rate

**Symptoms:** HighInferenceErrorRate; prediction 5xx above 5% with enough traffic.

**Dashboard:** Model Inference. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
sum(rate(airbnb_http_requests_total{endpoint="/predict",status=~"5.."}[5m])) / clamp_min(sum(rate(airbnb_http_requests_total{endpoint="/predict"}[5m])), 0.001)
```

**Diagnostics:**

```sh
kubectl -n airbnb-inference get rollout,pods
```

**Recovery:** Inspect the actual inference pod logs using kubectl logs <pod> -c inference --tail=100; avoid copying payloads or secrets. Check Redis, MLflow, validation and queue saturation. Roll back the model through the guarded promotion receipt, or revert the failing application commit in Git.

**Verification:** Require valid predictions at the intended version and a sustained error rate below the threshold.
