# AWS infrastructure

The platform is split into reusable Terraform modules and environment roots.
Only `terraform/environments/prod` is active; dev and staging are retained as
placeholders.

- `terraform/modules/ecr` creates immutable, scan-on-push image repositories. The environment creates one repository for the serving image and one for the training image.
- `terraform/modules/vpc` creates two-AZ public/private networking, NAT gateways, Kubernetes subnet tags, an S3 gateway endpoint, and private ECR, STS, and CloudWatch Logs endpoints.
- `terraform/modules/eks` creates an encrypted EKS cluster with audit logs, an EBS CSI driver role, explicit API access entries, and three managed node pools.
- `terraform/modules/route-53` can create or reuse the `5hort.site` hosted zone and DNS-validates a regional ACM certificate for the apex and wildcard domain. ExternalDNS owns the ALB record.
- `terraform/modules/rds`, `terraform/modules/redis`, and `terraform/modules/s3` provide separate MLflow/Airflow PostgreSQL stores, the Feast online store, and Object-Locked platform artifacts.

## Node-pool contract

The `system` pool hosts EKS add-ons. The `serving` and `training` pools are deliberately separate and tainted. A serving Deployment must select `workload=serving` and tolerate `workload=serving:NoSchedule`; a training Job must use the equivalent `training` selector and toleration. This prevents scheduled retraining workloads from consuming serving capacity.

Training defaults to CPU instances because this platform uses tabular models. Its desired size is zero in every environment to avoid paying for idle training nodes. Before scheduling a training Job, use a cluster autoscaler or scale that node group up through the deployment workflow. A GPU pool can be added later only if the chosen model genuinely needs it.

## State and deployment

The production environment has an empty S3 backend block. Bootstrap the
encrypted state bucket once, then use native S3 lock files without hard-coding
account-specific names:

```sh
cd terraform/environments/prod
terraform init \
  -backend-config="bucket=<terraform-state-bucket>" \
  -backend-config="key=prod-mlops/prod/terraform.tfstate" \
  -backend-config="region=eu-west-2" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
terraform plan
```

Use `terraform/environments/prod/terraform.tfvars.example` as the reviewed
input template. If `platform_admin_principal_arn` is left unset, the Terraform
caller receives the initial EKS administrator entry. The cluster API is private
by default; enable public access only with a restricted CIDR allow-list during
bootstrap.
