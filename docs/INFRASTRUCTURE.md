# AWS infrastructure

The platform is split into reusable Terraform modules and environment roots.
Only `terraform/environments/prod` is active; dev and staging are retained as
placeholders.

- `terraform/modules/ecr` creates immutable, scan-on-push image repositories. The environment creates one repository for the serving image and one for the training image.
- `terraform/modules/vpc` creates two-AZ public/private networking, NAT gateways, Kubernetes subnet tags, an S3 gateway endpoint, and private ECR, STS, and CloudWatch Logs endpoints.
- `terraform/modules/eks` creates an encrypted EKS cluster with audit logs, an EBS CSI driver role, explicit API access entries, and three managed node pools.
- `terraform/modules/route-53` creates or reuses the `platf0rm.site` hosted zone. ExternalDNS owns the apex NLB record; Kubernetes cert-manager issues and renews the HTTPS certificate used by Traefik.
- `terraform/modules/rds`, `terraform/modules/redis`, and `terraform/modules/s3` provide separate MLflow/Airflow PostgreSQL stores, the Feast online store, and Object-Locked platform artifacts.

## Rebuild inventory

Destroying EKS is safe only when the durable control and data planes remain.
The separate bootstrap S3 bucket retains Terraform state. The Object-Locked
platform bucket retains these rebuild inputs:

- `data/`: original source datasets needed to recreate transformations;
- `snapshots/`: immutable feature snapshots and lineage documents;
- `mlflow/`: model artifacts (the MLflow registry metadata remains in RDS);
- `production/events/`: immutable prediction events used for drift windows;
- `monitoring/`: persisted drift and model-quality reports; and
- `recovery/platform-manifest.json`: the non-secret inventory and expected prefixes.

Git remains the source for Terraform, Helm, Airflow DAGs, and dashboards; ECR
retains immutable serving/training images; Secrets Manager retains credentials.
Restore MLflow and Airflow metadata from RDS snapshots before bootstrapping the
applications. Redis is derived state and is repopulated from the immutable
feature snapshot. Do not treat S3 model artifacts alone as a registry backup.

## Node-pool contract

The `system` pool hosts EKS add-ons. The `serving` and `training` pools are deliberately separate and tainted. A serving Deployment must select `workload=serving` and tolerate `workload=serving:NoSchedule`; a training Job must use the equivalent `training` selector and toleration. This prevents scheduled retraining workloads from consuming serving capacity.

Training defaults to CPU instances because this platform uses tabular models.
Production keeps one on-demand training node for Airflow's LocalExecutor and its
control-plane components; the dev/staging placeholders may scale to zero. The
Cluster Autoscaler discovers the underlying Auto Scaling groups through explicit
Terraform-managed tags. Training work stays off the serving pool. A GPU pool can
be added later if the model needs it.

## Monitoring and observability architecture

Monitoring is a production control system, not only a set of operational
dashboards. Its purpose is to give engineering and business owners early,
actionable evidence that the platform is available, that predictions are
useful, and that automated decisions remain safe to operate.

### What is monitored

- **Customer-facing inference:** Prometheus scrapes the serving workload for
  request volume, latency, error rate, readiness, and model-version labels.
  These signals answer whether customers can obtain a price prediction within
  the expected service level and whether a release is safe to keep serving.
- **The model:** Every prediction is associated with the deployed model
  version and durable prediction events are retained in Object-Locked S3.
  Feature and prediction drift checks compare bounded production windows with
  reference data. When ground-truth outcomes become available, the model
  quality job calculates MAE, RMSE, R², bias, sample coverage, and business
  tolerance measures. This distinguishes an available service from a model
  that is becoming less accurate or less useful.
- **Training and data pipelines:** Airflow publishes success, failure,
  freshness, data-quality, drift, and retraining signals. Training runs and
  candidate models remain linked to MLflow, including metrics, parameters,
  artifacts, dataset identifiers, and the registered model version. This
  makes a production regression traceable to a dataset, code version, run,
  and deployment.
- **Platform servers and capacity:** The kube-prometheus-stack monitors EKS
  nodes, Kubernetes workloads, storage, and control-plane-adjacent services.
  Separate system, serving, and training pools make resource contention
  visible and protect inference capacity from scheduled training work.

### Monitoring components

Prometheus is the metrics and alert-evaluation layer. ServiceMonitor resources
discover the serving workload, while Airflow monitoring jobs publish durable
pipeline, drift, and model-quality metrics through the configured metrics
collector path. Grafana provides the operator and leadership view through
provisioned dashboards backed by Prometheus. Alertmanager routes actionable
alerts to the existing SNS notification path so incidents reach the on-call
team without requiring someone to watch a dashboard.

S3 is the durable evidence layer for prediction events, drift windows,
quality reports, and recovery metadata. MLflow is the experiment and model
lineage system: it explains which candidate, data version, and code produced
a model, while serving metrics show how that model behaves in production.
RDS retains MLflow and Airflow control-plane metadata. The backup policy
preserves the distinction between registry metadata and model artifacts.

### Business controls and response

The alerting model separates four decisions:

1. **Can customers use the service?** Availability, error, and latency alerts
   protect the prediction experience and revenue-dependent integrations.
2. **Is the platform operating reliably?** Pipeline failures, stale checks,
   capacity pressure, and missing telemetry identify operational risk before
   it becomes a customer incident.
3. **Is the data still representative?** Drift and data-quality alerts flag
   changes in the market or input process that can invalidate predictions.
4. **Is the model still delivering value?** Delayed outcome metrics measure
   real production quality and degradation against the configured reference.

Alerts are evidence for an operator decision, not an automatic licence to
promote a model. Canary analysis, immutable image digests, GitOps review, and
the existing model-promotion safeguards remain the change controls. A failed
or stale monitor therefore causes investigation or a hold, rather than being
silently treated as a healthy result.

This creates a clear chain from business risk to technical signal: customer
impact is observed at the API, model risk is measured against outcomes, and
infrastructure risk is tied to the servers and workflows supporting both. It
also keeps monitoring costs predictable by using the existing EKS,
Prometheus/Grafana, SNS, S3, RDS, and MLflow services.

## State and deployment

The production environment has an empty S3 backend block. Bootstrap the
encrypted state bucket once, then use native S3 lock files without hard-coding
account-specific names:

```sh
cd terraform/environments/prod
../../../scripts/terraform.sh init \
  -backend-config="bucket=<terraform-state-bucket>" \
  -backend-config="key=prod-mlops/prod/terraform.tfstate" \
  -backend-config="region=eu-west-2" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
../../../scripts/terraform.sh plan
```

The wrapper configures a user-level provider cache. This avoids storing another
copy of the approximately 800 MB AWS provider in every Terraform root module.

Use `terraform/environments/prod/terraform.tfvars.example` as the reviewed
input template. If `platform_admin_principal_arn` is left unset, the Terraform
caller receives the initial EKS administrator entry. The cluster API is private
by default; enable public access only with a restricted CIDR allow-list during
bootstrap.
