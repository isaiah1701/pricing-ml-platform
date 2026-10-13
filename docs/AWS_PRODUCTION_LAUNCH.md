# AWS production launch

This repository deploys one active environment: `prod`. The `dev` and
`staging` roots are retained as placeholders and are not part of the launch
workflow.

Repository pushes currently run validation only. AWS image publishing is
manual and disabled unless the GitHub repository variable
`AWS_PUBLISH_ENABLED` is explicitly set to `true`. Root and child Argo CD
Applications also require manual sync. Enable publishing and perform the
first Argo CD sync only when the production launch is intended.

## 1. Preflight

Use short-lived role credentials after bootstrap. The local `isaiahOct26`
profile is only the initial trusted operator.

```sh
export AWS_PROFILE=isaiahOct26
export AWS_REGION=eu-west-2
aws sts get-caller-identity
terraform fmt -check -recursive terraform
terraform -chdir=terraform/bootstrap/prod init -backend=false -input=false
terraform -chdir=terraform/bootstrap/prod validate
terraform -chdir=terraform/environments/prod init -backend=false -input=false
terraform -chdir=terraform/environments/prod validate
```

Copy both committed examples to ignored local inputs and replace the example
operator CIDR before applying:

```sh
cp terraform/bootstrap/prod/terraform.tfvars.example \
  terraform/bootstrap/prod/terraform.local.tfvars
cp terraform/environments/prod/terraform.tfvars.example \
  terraform/environments/prod/terraform.local.tfvars
```

## 2. Bootstrap remote state and GitHub OIDC

```sh
terraform -chdir=terraform/bootstrap/prod init
terraform -chdir=terraform/bootstrap/prod plan \
  -var-file=terraform.local.tfvars \
  -out=/tmp/mlops-bootstrap.tfplan
terraform -chdir=terraform/bootstrap/prod apply /tmp/mlops-bootstrap.tfplan
terraform -chdir=terraform/bootstrap/prod output -raw environment_backend_hcl \
  > /tmp/prod-backend.hcl
terraform -chdir=terraform/environments/prod init \
  -reconfigure -backend-config=/tmp/prod-backend.hcl
```

The bootstrap creates the encrypted, versioned state bucket, a scoped
Terraform execution role, the GitHub OIDC provider, and the production ECR
publishing role. Configure these GitHub repository variables:

- `AWS_REGION=eu-west-2`
- `AWS_ECR_PUBLISH_ROLE_ARN`, using the matching bootstrap output
- `AWS_PUBLISH_ENABLED=true`, only when ready to publish production images

Protect the GitHub `production` Environment with required reviewers and main
branch restrictions. Do not store AWS access keys in GitHub.

## 3. DNS delegation

Account `655916164714` did not contain the previously configured hosted zone.
The production root therefore creates the public zone by default. Create just
the zone first:

```sh
terraform -chdir=terraform/environments/prod apply \
  -var-file=terraform.local.tfvars \
  -target='module.route53.aws_route53_zone.this[0]'
terraform -chdir=terraform/environments/prod output route53_name_servers
```

Set those name servers at the domain registrar and verify public delegation.
If DNS is already hosted in this account, set
`create_route53_hosted_zone=false` and provide `route53_hosted_zone_id`
instead. Never create a second authoritative zone without delegating it.

## 4. Provision production infrastructure

```sh
terraform -chdir=terraform/environments/prod plan \
  -var-file=terraform.local.tfvars \
  -out=/tmp/mlops-prod.tfplan
terraform -chdir=terraform/environments/prod show /tmp/mlops-prod.tfplan
terraform -chdir=terraform/environments/prod apply /tmp/mlops-prod.tfplan
```

The production apply creates:

- two-AZ VPC networking, private EKS nodes, NAT gateways, and VPC endpoints;
- immutable ECR repositories for serving and training images;
- separate Multi-AZ PostgreSQL instances for MLflow and Airflow, with RDS-managed credentials, backups, final snapshots, and deletion protection;
- Multi-AZ encrypted ElastiCache Redis for Feast;
- a versioned, TLS-only, S3 Object-Locked data and artifact bucket;
- ACM and Route 53 resources; and
- least-privilege EKS Pod Identity associations for workloads and controllers.

Render the non-secret AWS endpoints and ARNs into the GitOps values:

```sh
uv run python scripts/render_aws_deployment_values.py
```

Review and commit the resulting Helm values. The renderer never reads or
writes database passwords.

## 5. Seed data and publish images

Upload the raw source object before Airflow starts:

```sh
PLATFORM_BUCKET="$(terraform -chdir=terraform/environments/prod output -raw platform_data_bucket)"
aws s3 cp data/AB_NYC_2019.csv \
  "s3://${PLATFORM_BUCKET}/data/AB_NYC_2019.csv"
```

Push the rendered values, then run `Publish immutable images and propose
GitOps promotion` manually from GitHub Actions. It scans both images, pushes only `sha-<git-sha>` tags to
the Terraform-created ECR repositories, and opens a promotion pull request.
Merge that pull request only after CI succeeds.

## 6. Bootstrap GitOps

Cluster access is private by default. During initial setup, use the restricted
operator CIDR from the production tfvars or run these commands from an in-VPC
runner. Remove public API access after private administration is available.

```sh
aws eks update-kubeconfig --name prod-mlops-prod --region eu-west-2
helm repo add argo https://argoproj.github.io/argo-helm
helm upgrade --install argocd argo/argo-cd \
  --version 10.10.3 \
  --namespace argocd \
  --create-namespace \
  --set configs.params.server\.insecure=true
kubectl apply -f serving/argocd/root-application.yaml
```

Manually sync the root Application, then the controller, monitoring, platform,
and inference Applications in order. Automatic sync is disabled by default.
Enable it explicitly later if desired; a Git push alone does not launch AWS.

Argo CD then installs Argo Rollouts, External Secrets Operator, AWS Load
Balancer Controller, ExternalDNS, Metrics Server, Cluster Autoscaler,
kube-prometheus-stack, Pushgateway, MLflow, Airflow, and the inference
Rollout.

## 7. Create the first governed champion

The production MLflow registry starts empty. Port-forward Airflow or use an
approved internal access path, trigger `airbnb_feature_build`, and then trigger
`airbnb_model_training`. The training DAG:

1. validates and freezes the dataset;
2. records code, data, attempt, and model hashes;
3. reload-verifies the logged artifact;
4. applies the evaluation gate;
5. registers a new model version; and
6. moves `airbnb-price-predictor@champion` only after all metadata is written.

Inference intentionally remains unready until that champion exists. Its
refresh loop loads the champion without an image redeployment.

## 8. Launch and rollback gates

Before opening DNS traffic, verify all pods, ExternalSecrets, PVCs,
ServiceMonitors, dashboards, and alerts. Run a gradual load test and confirm
that saturation returns retryable HTTP 503 responses without restarting pods.

Argo Rollouts progresses through 5%, 25%, 50%, and 100%. Error-rate or p95
latency failures abort the candidate and preserve the stable ReplicaSet. A code
rollback reverts the GitOps image commit; a model rollback moves the MLflow
`champion` alias to the previous governed version.
