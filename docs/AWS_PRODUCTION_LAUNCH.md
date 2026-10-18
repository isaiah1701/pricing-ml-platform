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

Newer GitHub repositories use immutable OIDC subjects. Set
`github_oidc_repository="OWNER@OWNER_ID/REPOSITORY@REPOSITORY_ID"` in the bootstrap
tfvars when the repository uses that format. Use the exact IDs, not wildcards;
the publishing role remains restricted to the `production` Environment.

## 3. DNS delegation

Production uses `platf0rm.site` itself for the application and HTTPS certificate.
The production root creates its Route 53 public zone by default. Create just
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
- Route 53 resources (cert-manager manages HTTPS certificates in Kubernetes); and
- least-privilege EKS Pod Identity associations for workloads and controllers.

Render the non-secret AWS endpoints and ARNs into the GitOps values:

```sh
uv run python scripts/render_aws_deployment_values.py
```

Review and commit the resulting Helm values. The renderer never reads or
writes database passwords.

The renderer obtains each database's host, port, and name from Terraform outputs;
External Secrets reads only `username` and `password` from RDS-managed secrets.
Database connections require TLS. Feast uses the AWS-specific
`feature_store.aws.yaml`, which selects the Terraform-rendered `REDIS_TYPE`
(`redis` for the budget standalone node, `redis_cluster` otherwise) and the Feast connection
format `endpoint:6379,ssl=true` (not a Redis URL).

External Secrets refreshes database credentials every five minutes. Reloader
restarts the annotated MLflow and Airflow deployments when those Secrets change,
so RDS-managed password rotation reaches processes that consume environment
variables. Airflow's single-pod topology briefly interrupts scheduling during a
restart; time rotations accordingly and verify the component probes recover.

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

If repository policy disallows Actions-created pull requests, dispatch with
`create_promotion_pr=false`. This still builds, runtime-checks, scans, and publishes
the images. An operator then updates the immutable Helm image coordinates, runs
the launch preflight below, and commits the reviewed promotion. No repository
permission weakening or automatic Kubernetes deployment is required.

The publishing workflow checks the complete values file and verifies both tags
in ECR before creating the promotion PR. Run the same launch preflight locally:

```sh
uv run python scripts/validate_aws_deployment.py --check-images
```

Production child Applications enable a Helm launch gate that refuses unresolved
coordinates and placeholder images. Offline CI rendering leaves the gate disabled
so templates can be validated before AWS provisioning.

## 6. Bootstrap GitOps

Cluster access is private by default. During initial setup, use the restricted
operator CIDR from the production tfvars or run these commands from an in-VPC
runner. Remove public API access after private administration is available.

Use the protected manual **Bootstrap** workflow after reviewing the production
Terraform coordinates and existing images. It installs the existing Argo CD
release through `helmfile.yaml`, preserving the release's current budget values
and downstream Argo ownership. It validates account/cluster/node prerequisites
before Kubernetes changes and preserves healthy installations by default.
See [bootstrap.md](bootstrap.md) for exact variables, gates, ownership and offline
validation. Do not trigger this workflow for ordinary application changes.

Manually sync the root Application, then the controller, monitoring, and platform
Applications. On an empty registry, complete section 7 before syncing inference;
this prevents the initial Rollout from timing out while waiting for its first
champion. Manual Kubernetes sync operations must include
`syncOptions: ["CreateNamespace=true", "ServerSideApply=true"]` so namespaces and
large CRDs are created correctly. Automatic sync is disabled by default.
Enable it explicitly later if desired; a Git push alone does not launch AWS.

Argo CD then installs Argo Rollouts, External Secrets Operator, AWS Load
Balancer Controller, ExternalDNS, Metrics Server, optional Cluster Autoscaler,
kube-prometheus-stack, Pushgateway, MLflow, Airflow, and the inference
Rollout.

HTTPS uses Traefik behind an internet-facing NLB. cert-manager installs its CRDs
and issues the inference Certificate through a namespaced Let's Encrypt Issuer
using HTTP-01 and the `traefik` ingress class. ExternalDNS publishes the hostname
from the Traefik LoadBalancer Service. Delegate the Route 53 zone and allow public
ports 80 and 443 before issuance. Traefik redirects ordinary HTTP requests to HTTPS;
the redirect has low priority so temporary ACME challenge routes take precedence.
Sync the AWS load balancer controller, cert-manager, and Traefik before inference.
Verify `kubectl -n airbnb-inference get certificate,issuer,order,challenge` and wait
for the Certificate to become Ready before launching. For testing, set
`ingress.acmeServer=https://acme-staging-v02.api.letsencrypt.org/directory`; staging
certificates are not browser-trusted. No ACM certificate is needed.

## 7. Restore the approved champion before considering training

Airflow runs API server, scheduler, DAG processor, and triggerer in separate
containers in one pod, using LocalExecutor and the shared RWO data volume. The
single pod uses Recreate upgrades and is not a highly available Airflow topology.
The budget profile shares one on-demand worker across the platform. Component-specific startup
and health probes supervise each process; database and FAB migrations run first.
DAGs start paused so monitoring workflows are enabled only after their production
feature/outcome files are supplied in `/opt/airflow/input`.

The FAB auth manager provides database-backed user login; the API remains internal.
External Secrets generates persistent runtime signing/encryption keys and a random
initial admin password in the `airflow-runtime` Secret. Retrieve that password using
an approved secret access path; never commit or copy it into logs. Back up this
Secret securely with database backups: losing the Fernet key prevents decrypting
stored Airflow connections. Reset user passwords through FAB rather than deleting
the runtime Secret. The initial admin identity can be configured with
`platform.airflow.adminUsername` and `platform.airflow.adminEmail`.

A recreated MLflow database can start empty even when retained S3 artifacts
exist. Verify and restore the approved recovery bundle as documented in
[model-recovery.md](model-recovery.md); this works without the original RDS
snapshot. Run the data DAG to recover offline/Redis features if needed, then
select the restored champion through ML Deploy. Do not trigger training as an
implicit launch/recovery step. Existing local runbooks that say to train at every
launch must follow this recovery branch first.

Before any teardown, the current production model needs a one-time export and
read-only verification in the new recovery format. This has not been performed
by the CI/CD implementation session. PVC CatBoost snapshots alone do not provide
durable disaster recovery when the volume is deleted.

Only when no approved recoverable model exists, and after an explicit training
request, use Airflow through an approved internal access path: trigger
`airbnb_feature_build`, then `airbnb_model_training`. The training DAG:

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
