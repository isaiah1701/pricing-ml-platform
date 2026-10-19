# Grafana or Prometheus unavailable

**Symptoms:** Dashboards unavailable, query errors, missing scrape data or alert evaluation errors.

**Dashboard:** Kubernetes & Delivery. Owner: ML Platform on-call; involve Data Science for
model-quality decisions and SRE for cluster/delivery faults.

**PromQL** (scope to production when querying a shared Prometheus):

```promql
up{namespace="monitoring"}
```

**Diagnostics:**

```sh
kubectl -n monitoring get pods,pvc,svc
kubectl -n monitoring get events --sort-by=.lastTimestamp
```

**Recovery:** Check existing monitoring pod resources, PVC capacity, scrape discovery and Grafana provisioning. Port-forward to the existing Services through an approved access path. Repair via GitOps; do not add datasources or Grafana S3 access. Recover Pushgateway snapshots after replacement. Monitoring cannot notify after its own cluster disappears: use the documented planned-maintenance silence beforehand.

**Verification:** Require healthy scrape targets, successful dashboard queries and alert evaluations. Test email via the configured contact point only when explicitly authorized; delivery is a separate live gate.
