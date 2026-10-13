# Kubernetes and GitOps deployment definitions

The complete, production-only execution order is documented in
`docs/AWS_PRODUCTION_LAUNCH.md`. Terraform and Helm remain reviewable until the
operator explicitly runs `apply` and installs the Argo CD root Application.

Pushing this repository runs validation only. AWS publishing requires a manual
workflow dispatch and `AWS_PUBLISH_ENABLED=true`; automatic Argo CD sync is
disabled on the root and child Applications until deliberately enabled.

## Delivery and runtime flows

Model flow:

```text
training -> MLflow experiment -> Registry @champion -> FastAPI -> Docker -> Helm -> EKS
```

GitOps flow:

```text
Git -> root Argo Application -> AppProject + child Applications -> Helm -> EKS
```

Secret flow:

```text
AWS Secrets Manager -> External Secrets Operator -> ClusterSecretStore
-> ExternalSecret -> Kubernetes Secret -> inference pod
```

The root manifest is `serving/argocd/root-application.yaml`. It installs the
same Helm chart in bootstrap mode. Bootstrap mode renders an `AppProject` and
three child Applications but no inference workload:

- `external-secrets` at wave `-2` installs the pinned ESO chart and CRDs. Its
  controller service account is the subject of the Terraform EKS Pod Identity
  association.
- `airbnb-inference-secrets` at wave `-1` renders only the
  `ClusterSecretStore` and `ExternalSecret` from this repository. The operator
  therefore exists before its custom resources.
- `airbnb-inference` at wave `0` renders the Deployment, Service,
  ServiceAccount, ConfigMap, and HPA, while the separate secrets child owns the
  Kubernetes Secret lifecycle.

Every child has the Argo resource finalizer, configurable source repository,
revision, destination cluster/namespace, automated sync, pruning, self-healing,
and `CreateNamespace=true`. With `bootstrap.enabled=false`, ordinary Helm
rendering produces normal workload resources.

## Helm validation

```sh
helm lint serving/helm -f serving/helm/values/base.yaml

helm template airbnb-inference serving/helm \
  --namespace airbnb-inference \
  -f serving/helm/values/base.yaml \
  -f serving/helm/values/production.yaml \
  > /tmp/airbnb-workload.yaml

helm template prod-mlops-bootstrap serving/helm \
  --namespace argocd \
  -f serving/helm/values/bootstrap.yaml \
  > /tmp/airbnb-bootstrap.yaml

kubeconform -strict -summary -ignore-missing-schemas /tmp/airbnb-workload.yaml
kubeconform -strict -summary -ignore-missing-schemas /tmp/airbnb-bootstrap.yaml
```

The `ignore-missing-schemas` flag is needed for External Secrets and Argo CD
custom resources. Review the output before any cluster sync. Replace the
production ECR placeholder and `sha-REPLACE_WITH_GIT_SHA` with an immutable,
scanned image reference; production must never use `latest`.

## Terraform integration

The repository already organizes AWS code under `terraform/modules` and
`terraform/environments`, so no duplicate `serving/terraform` tree was added.
The Secrets Manager resource follows the reference project's write-only
`secret_string_wo` pattern, adapted as `terraform/modules/secrets-manager` to
match this repository's reusable-module convention. Production currently
creates the secret container without a value, keeping plaintext and secret
material out of Git and normal Terraform plans/state.

The production environment defines:

- an ESO role limited to `DescribeSecret` and `GetSecretValue` on exactly the
  inference secret;
- an EKS Pod Identity association for the `external-secrets` controller;
- an inference role limited to `ListBucket`, `GetObject`, and `GetObjectVersion`
  for the MLflow artifact prefix;
- workload identities for MLflow, Airflow, ExternalDNS, Cluster Autoscaler, and
  AWS Load Balancer Controller;
- Multi-AZ PostgreSQL and Redis, plus an Object-Locked S3 artifact bucket; and
- GitOps-managed MLflow, Airflow, Prometheus, Grafana, and Pushgateway workloads.

Static validation only:

```sh
terraform fmt -check -recursive terraform
terraform -chdir=terraform/environments/prod init -backend=false -input=false
terraform -chdir=terraform/environments/prod validate
terraform -chdir=terraform/environments/prod plan
```

Do not run `apply` until the following cloud-specific inputs and dependencies
have been verified:

- ECR repository URL and immutable image SHA;
- reachable production MLflow tracking endpoint and its authentication mode;
- whether MLflow artifacts use S3, plus the exact bucket ARN and prefix;
- the runtime secret JSON properties expected by `production.yaml`;
- EKS metrics-server availability for HPA CPU metrics;
- Argo CD installation and root-application repository access;
- External Secrets Operator compatibility with the selected Kubernetes version;
- network paths/DNS/TLS from EKS to MLflow, Secrets Manager, STS, and S3;
- actual service sizing and CatBoost model startup latency.

The CI workflow performs tests, linting, a Docker build, Helm rendering,
kubeconform checks, Terraform formatting, and `terraform validate`. It neither
pushes an image nor authenticates to AWS, mutates Git image references, deploys
with `kubectl`, or runs Terraform apply.
