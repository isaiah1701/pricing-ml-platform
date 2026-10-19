# V1 operational hardening report

Implementation is complete and validated offline. Deployed V1 acceptance remains
pending the live gates below. No AWS changes, EKS deployments, model promotions,
production failure injection, email, training or load tests were executed. No
push was performed; unrelated README, load-tests and skills work is preserved.

| Owner | Delivered |
| --- | --- |
| Observability specialist | Serving refresh/champion/registry metrics; repaired existing three dashboard UIDs; new Pipeline Health dashboard; real pod-hash stable/canary traffic joins; producer/query checks |
| Drift/alerting specialist | Completed-report-first publication; S3 latest state and fixed Pushgateway jobs; replay; persistent/idempotent counters; eight Grafana alerts; optional secure SMTP/contact policy; synthetic alerts |
| Reliability specialist | Shared-PVC promotion receipts/locks; provenance/checksum validation; interrupted/stale-operation guards; verified serving rollback; pending metric-event outbox; canary fail-closed checks and stable retention; scoped GitOps routing ownership |
| Lead | Audit/metric contract, DAG integration, training preserves existing champion, actual KSM collectors/RBAC, explicit aborted flag, optional exact-ARN SMTP permission on existing role, integration tests, CI checks, runbooks and review |

## Dashboards and metrics

All four JSON dashboards are mirrored into the Helm chart and GitOps-provisioned:
Model Inference; Model Quality & Drift; Pipeline Health; Kubernetes & Delivery.
Existing UIDs remain stable. Offline holdout MAE/RMSE/R² is clearly separated from
delayed production outcomes; absence of matched ground truth is unavailable data.

The [metric contract](metric-contract.md) lists producers, labels, types and
freshness. New measurements cover refresh failure/last success/champion/registry
availability; completed drift share/threshold/failures; DAG outcome/duration/last
success/failures; offline champion/challenger scores; verified promotion/rollback
lifecycle; actual Rollout phase/revisions/abort and AnalysisRun phases. Existing
HTTP/latency/readiness, Kubernetes and delayed-outcome metrics remain.

Detailed drift JSON/HTML stays in S3. Numeric history is Prometheus time series
(current retention two days); recoverable latest snapshots live in the existing
S3 bucket. Grafana has only its existing Prometheus datasource. No new AWS
service, Grafana AWS role, database, query engine or static credentials were added.

## Alerts and email

Eight Grafana-managed alerts are provisioned: InferenceDown,
HighInferenceErrorRate, HighInferenceLatency, ModelDriftDetected,
DriftMonitoringStale, AirflowPipelineFailure, ModelRefreshFailure and
CanaryRolloutFailure. Availability/freshness handles absence; error/latency have
traffic guards. Duplicate legacy serving/drift alerts were removed; unrelated
capacity/outcome/data-quality rules retain their existing Alertmanager routing.

The optional `mlops-email` contact point and notification policy use a reviewed
recipient and External Secrets SMTP credentials. No credential is committed.
The exact existing SMTP secret ARN can be granted read-only access through
`grafana_smtp_secret_arn` on the existing External Secrets IAM role; empty default
changes no permissions. See [setup, delivery test and maintenance silence](drift-alerting.md).

## Rollback and recovery

Promotion requires approval, verified source/artifacts and an unchanged previous
champion. Durable receipts serialize cooperating writers and resume the original
operation after interruption. Alias restoration is followed by concrete-version
readiness/prediction/latency verification; only then is rollback complete.
Telemetry outages preserve pending events without changing verified model outcomes.

Canaries retain stable capacity, require >=20 requests per two-minute analysis,
and reject missing/NaN/breaching results. GitOps respects the routing fields owned
by Rollouts. Tests establish these contracts; they do not run an Argo controller
or prove live stable-traffic restoration. Use the [rollback procedure](rollback.md)
and [incident runbooks](../runbooks/README.md), including the reviewed Git revert.

Existing S3 bundle verification and empty-registry recovery remain functional
without retraining/RDS snapshots. Current production champion still requires the
one-time export and independent verify before teardown. Promotion leases and
training checkpoints on the Airflow PVC are not durable teardown recovery state.
Privileged external MLflow writers cannot be atomically excluded by MLflow's API;
restrict them to the cooperating workflows. Endpoint checks sample serving rather
than asserting every replica has refreshed.

## Validation

- Final complete Python regression: **239 passed**, 14 dependency warnings.
- **17 focused final configuration tests passed**, covering the explicit aborted flag, actual
  metric producers, provisioned/synthetic alert equality, report upload ordering,
  all critical DAG callbacks, notification receiver references and dashboard copies.
- Ruff, actionlint, diff checks, default/production/bootstrap Helm lint pass.
- Promtool parses 61 dashboard expressions, nine retained Prometheus alerts and
  eight Grafana query equivalents; synthetic drift resolution/stale/absence,
  traffic guards and duplicate-scrape/stable-canary tests pass.
- Actual pinned monitoring chart 92.3.0 renders KSM collector config/RBAC and
  Grafana provisioning. Local kubeconform validates supported resource schemas;
  custom-resource schemas are skipped (18 valid, 33 skipped, zero invalid in the
  platform/inference/bootstrap renders). CRD behavior remains a live gate.
- Terraform production formatting and backend-free validate pass for optional
  SMTP exact-ARN permission; saved-plan/account/backend guards remain tested.
- Existing eight workflows retain their names/responsibilities. Docker's existing
  validation job now also runs the Grafana synthetic checks; no workflow was added.

Early incomplete callback/fixture checks and one YAML indentation error were
corrected; no known test failures remain. Local Docker image builds and a Grafana
server evaluation were not run. CI and live acceptance must verify those runtimes.

## Commits and live gates

- `6131eef`: serving instrumentation.
- `c6541b5`: durable drift, dashboards and alert/email configuration.
- `d769d88`: guarded promotion/verified rollback and canary/GitOps safety.
- Final integration/runbook commit: find with
  `git log -1 --grep='complete V1 observability integration and incident runbooks'`.

Before live sign-off: review/approve runtime image and GitOps releases; configure
SMTP secret/recipient/exact-ARN permission and restart Grafana after sync; replay
retained metric states and run one approved drift evaluation; verify Prometheus
scrapes, dashboard values and alert breach/resolution; authorize/test email;
verify concrete model refresh/rollback and stable canary traffic; export/verify
the current champion. Preserve the protected environments and reviewed Terraform
handoff. The portfolio implementation is V1 complete; deployed operational
verification and live portfolio sign-off are outstanding.

Configuration references: [Grafana file provisioning](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/file-provisioning/)
and [KSM custom-resource metrics](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/extend/customresourcestate-metrics.md).
