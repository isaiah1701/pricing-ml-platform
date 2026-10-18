# Canary rollout failure

**Symptoms:** CanaryRolloutFailure; failed/inconclusive analysis or degraded Rollout.

**Dashboard:** Kubernetes & Delivery. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
mlops_analysis_run_info{namespace="airbnb-inference",phase=~"Failed|Error|Inconclusive"}
```

**Diagnostics:**

```sh
kubectl -n airbnb-inference get rollout,analysisrun,rs,pods
kubectl -n airbnb-inference describe rollout airbnb-inference
kubectl -n airbnb-inference get svc airbnb-inference airbnb-inference-canary -o wide
```

**Recovery:** Inspect analysis samples, query reachability, errors and latency. Automatic abort must preserve stable traffic; missing traffic does not approve a canary. Revert the failing image/config commit in Git, review the restored immutable digest and use Kubernetes to reconcile the revert. Avoid direct Helm or patching Git-managed image fields.

**Verification:** Confirm stable Service/Traefik weights, stable ReplicaSet ready pods, healthy Rollout at the reviewed revision and successful predictions. Retained failed analyses may remain visible until history retention removes them.
