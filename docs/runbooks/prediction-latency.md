# High inference latency

**Symptoms:** HighInferenceLatency; p95 over 250 ms with enough prediction samples.

**Dashboard:** Model Inference / Kubernetes & Delivery. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
histogram_quantile(0.95, sum by (le) (max without (job, service) (rate(airbnb_prediction_latency_seconds_bucket[5m]))))
```

**Diagnostics:**

```sh
kubectl -n airbnb-inference top pods
kubectl -n airbnb-inference get rollout,pods
```

**Recovery:** Compare CPU, memory, queue rejection and canary revision. Inspect dependency latency and recent model changes. Restore the approved previous model or revert the application revision after identifying the cause. Do not enable autoscaling or add nodes as an implicit fix.

**Verification:** Verify p95 recovers under representative existing traffic and queue rejections stop; do not run a production load test without approval.
