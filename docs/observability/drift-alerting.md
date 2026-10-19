# Drift metrics and Grafana notifications

S3 → Airflow → the existing Pushgateway → Prometheus → Grafana is the only
operational path. Grafana has no S3 credentials or datasource. Detailed JSON/HTML
reports remain under `monitoring/drift/<evaluation timestamp>/` in the existing
platform data bucket. Feast offline Parquet and online Redis are unchanged.

The daily `airbnb_drift_monitoring` DAG evaluates a bounded production window.
It uploads both reports before publishing the completed numeric result. Failed
or in-progress evaluations retain the previous completed result. The existing
branch emits retraining requests; approval is still required for promotion.

`monitoring/metric-state/{drift,model-quality,operational}/latest.json` retains
recoverable numeric state in the same S3 bucket. Conditional S3 writes prevent
lost updates; publishers serialize S3 read/write and Pushgateway replacement
using `AIRBNB_BATCH_METRIC_STATE_DIR` on Airflow's shared PVC. All publishers and
replay must use that same shared directory. Each fixed job is replaced, rather
than creating run-specific Pushgateway grouping keys. Counters and hashed audit
IDs survive task exit, restart and retries; audit IDs are never metric labels.
The deduplication ledger deliberately retains event hashes rather than dropping
old retry protection. Its size grows with terminal runs/events, not scrape count.
A failed HTTP publication leaves S3 committed so retry/replay restores it.

The existing Pushgateway chart 3.9.1 mounts its 1 Gi PVC at `/data`; explicit
`--persistence.file=/data/pushgateway.data` and `--persistence.interval=1m` preserve
it across pod restarts. S3 replay covers loss of that PVC. The existing
ServiceMonitor discovers it and preserves pushed job labels. No new service,
database, IAM role, query engine or infrastructure is introduced.

After infrastructure recovery, execute in the existing Airflow runtime, with its
existing pod identity, platform bucket, Pushgateway URL and shared metric path:

```sh
python -m monitoring.operational_metrics replay
```

This reads three latest objects and republishes their original evaluation
timestamps. It does not scan historical reports, read feature datasets, retrain,
or make stale results look fresh. Unknown metric names, unexpected label keys,
nonfinite values and excessive series counts are rejected. Inspect with:

```sh
kubectl -n monitoring port-forward svc/prometheus-pushgateway 9091:9091
curl -fsS localhost:9091/metrics
```

Healthy drift is `airbnb_dataset_drift == 0` plus a fresh last-success timestamp.
A breach is `== 1` with a fresh timestamp. Older than 26 hours is stale, and an
absent success timestamp is no data; neither absence nor stale results mean
healthy. Failure callbacks increment durable drift/pipeline counters without
erasing the last success. Feature names/kinds and existing model-version info
remain bounded by the configured monitoring schema; run/customer/request IDs
never become labels. Offline holdout metrics explicitly use champion/challenger
roles and do not claim live production accuracy. Production accuracy remains
unavailable until matched outcome data exists.

## Alert ownership and notification configuration

Production email uses **Grafana → the existing Alertmanager → the existing
SNS topic → imichael1403@gmail.com**. It requires no SMTP credentials or Grafana
AWS role. The production notification mode is `alertmanager`; Terraform owns the
email subscription and the existing publisher role's exact SNS/KMS permissions.
AWS sends a subscription confirmation link that the recipient must accept before
alerts can arrive. Required confirmation is not automatically bypassed.

The Alertmanager route explicitly defines its Watchdog `null` receiver; inheriting
a default child route while replacing its receiver list otherwise prevents the
operator from starting Alertmanager. Grafana production alerts retain the seven-
day repeat policy through both routing layers. SMTP below remains an optional
alternative for deployments that already have an SMTP service.

Grafana sidecar provisioning installs exactly eight managed rules:
InferenceDown, HighInferenceErrorRate, HighInferenceLatency, ModelDriftDetected,
DriftMonitoringStale, AirflowPipelineFailure, ModelRefreshFailure and
CanaryRolloutFailure. `monitoring/grafana/alert-rules.yml` is mirrored into the
Helm chart. Prometheus remains their datasource (`prometheus` UID).

Legacy serving/drift Prometheus alerts for these conditions were removed to
avoid duplicate Grafana/SNS notifications. Unrelated capacity, event persistence,
matched-outcome and data-quality alerts retain the existing Alertmanager route.
The latency/error rules require at least one prediction per second over five
minutes. Inference and stale-monitoring absence alerts deliberately; other rules
treat no data as OK and rely on availability/freshness alerts. Query errors enter
Grafana's Error state. Canary queries use explicitly configured kube-state-metrics
custom resource series rather than assumed controller metrics.

For the optional direct-SMTP route, supply a reviewed recipient and working
SMTP secret. In `values/production.yaml` configure:

```yaml
platform:
  grafanaAlerting:
    notifications:
      enabled: true
      mode: smtp
      email: platform-operators@example.com
      smtpSecretArn: <existing SMTP SecretsManager secret ARN>
```

Set Terraform production input `grafana_smtp_secret_arn` to that same existing
ARN. The default empty input changes no permissions; a reviewed saved plan can
grant only DescribeSecret/GetSecretValue for this exact ARN to the existing
External Secrets role. Apply requires separate explicit authorization. No new
role or wildcard permission is introduced. The existing platform ClusterSecretStore
must have that permission before notification configuration is enabled. The JSON secret needs
`host` (for example SMTP endpoint plus `:587`), `username`, `password` and
`from_address`. External Secrets supplies `GF_SMTP_*` environment variables to
Grafana, requires STARTTLS, and retains the Kubernetes Secret. The Grafana chart
references this secret optionally so the default deployment works before SMTP
is configured. Restart Grafana through a reviewed GitOps pod-template change
after initial creation or secret rotation; environment variables do not refresh
inside running processes. Credentials never enter Helm values or ConfigMaps.

The provisioned `mlops-email` contact point and root notification policy share
an exact receiver name. Groups use alert name/service/environment, with 30-second
initial grouping, five-minute group updates and a seven-day repeat interval.
Unchanged drift evaluation identities do not include timestamps/model versions,
so scrapes and retries do not create duplicate alert instances; resolution is
sent when a new healthy evaluation arrives. Persisted long-lived breaches may
repeat after seven days by design.

For an authorized delivery test, use Grafana **Alerting → Contact points →
mlops-email → Test** after GitOps reconciliation and Grafana restart. Confirm one
email reaches the reviewed inbox, inspect delivery errors without revealing
credentials, and record time/result. This implementation has not sent live email.

Before planned teardown, create a Grafana silence matching
`environment=production`, `service=airbnb-price-predictor`, expiring after the
maintenance window, and silence relevant legacy Alertmanager routes separately.
Remove the silence only after Prometheus, serving and drift replay are verified.
This is maintenance suppression, never a permanent disabling of rules.

Runbooks are linked from every alert. Grafana/Prometheus failure diagnosis is in
[monitoring unavailable](../runbooks/monitoring-unavailable.md). Deployment and
live SMTP/dashboard/failure-injection checks remain separate acceptance gates.

## Offline checks

```sh
pytest tests/test_operational_metrics.py tests/test_grafana_alerting.py tests/test_drift_check.py tests/test_monitoring_dag.py
promtool test rules tests/monitoring/grafana-alerts-test.yml
```

The synthetic Prometheus rules mirror Grafana A queries and B conditions; tests
cover breach/resolution, stale/absent monitoring, healthy/unavailable serving,
and sparse versus sustained error traffic. These checks do not contact AWS,
a cluster or an SMTP server.
