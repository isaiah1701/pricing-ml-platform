# V1 source audit

Baseline: `a4ec644`; exactly eight CI/CD entry workflows and manual Helmfile/Argo
bootstrap were committed. Existing uncommitted README, load-tests and skills work
is preserved. This is a source/offline audit, not a statement of deployed health.

| Area | Existing implementation | Gap addressed in this hardening |
| --- | --- | --- |
| Inference | HTTP status counters, latency histogram, concrete loaded model info, readiness, bounded queue metrics | Refresh failures swallowed by loader, no approved alias target metric; missing p50/p99 and lifecycle views |
| Dashboards | Three GitOps JSON dashboards and mirrored chart copies; scrape deduplication | Dedicated pipeline view missing, operational state gaps, quality must distinguish delayed labels from offline evaluation |
| Drift | Daily Evidently DAG, fixed-window production events, JSON/HTML S3 history, feature PSI and gauges | Metrics published before durable report completion; persistence/recovery contract and failure counter missing |
| Batch metrics | Existing Pushgateway release with PVC and ServiceMonitor | Need explicit snapshot persistence validation, durable S3 latest state/replay, fixed grouping and idempotent updates |
| Pipeline health | Kubernetes container/replica health | Does not establish DAG outcomes, last successful run or duration |
| Alerting | PrometheusRule alerts and SNS routing | Grafana-managed eight-rule set/email provisioning missing; duplicate ownership and traffic guards need resolving |
| Model promotion | Evaluation/human approval, previous alias, provenance checks in manual selection | Retraining path switches alias before audit tags, no shared writer lease, rollback does not verify serving reload |
| Canary | 5/25/50 progression, traffic routing, error/p95 analysis | Empty/NaN results pass; no explicit minimum samples; rollout/analysis state needs real collector configuration |
| Recovery | Content-addressed S3 bundle, integrity checks, empty-registry restore tests | Production bundle still needs export/verify; infrastructure recovery and rollback must remain separate |

Implementation uses the [shared metric contract](metric-contract.md). Detailed
reports remain in S3, numeric history in Prometheus (current retention two days).
Grafana does not query S3. No Grafana AWS role, extra datasource, query engine or
new database is introduced. Planned teardown must silence notification routes
before the monitoring stack disappears.
