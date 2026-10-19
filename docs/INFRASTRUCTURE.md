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
