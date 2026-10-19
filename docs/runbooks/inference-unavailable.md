# Inference unavailable

**Symptoms:** InferenceDown; no ready replicas or no scrape targets.

**Dashboard:** Model Inference / Kubernetes & Delivery. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
sum(max without (job, service) (airbnb_readiness))
```

**Diagnostics:**

```sh
kubectl -n airbnb-inference get pods,svc,endpoints,rollout
kubectl -n airbnb-inference describe rollout airbnb-inference
```

**Recovery:** Check pod events, secrets, features/Redis and registry reachability. Restore the existing healthy revision through the reviewed Git revert and Argo sync process. If the model alias is missing, follow registry recovery; do not retrain.

**Verification:** Require healthy desired pods, /ready at champion, a valid prediction and recovered Prometheus scrapes.
