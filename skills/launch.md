# Launch production infrastructure

When asked to execute this file, launch this repository's `prod` environment.
Work from the repository root. Do not launch `dev` or `staging`.

1. Read `AGENTS.md`, `docs/AWS_PRODUCTION_LAUNCH.md`, and
   `docs/COST_OPTIMIZED_DEPLOYMENT.md`. Follow the production launch guide in
   order; it contains the Terraform, image publication, GitOps, and first-model
   commands. Keep the existing budget configuration.
2. Check AWS identity with `aws sts get-caller-identity`, using the intended
   account and `eu-west-2`. Required tools are AWS CLI, Terraform, `kubectl`,
   Helm, and `uv`; GitHub access is needed for image publication and promotion.
   Use `scripts/terraform.sh` in place of `terraform` to share the provider cache.
3. Reuse existing bootstrap state, remote environment state, and ignored
   `terraform.local.tfvars` files. Copy an example only when the corresponding
   local file is missing. Replace placeholder inputs, especially the operator
   CIDR, before applying. Never overwrite existing inputs or create a fresh
   backend to work around missing state. Resolve missing credentials, state, or
   account identity before provisioning.
   Verify the current public egress IPv4 against the EKS operator allowlist;
   update only verified `/32` entries if the operator's network has changed.
4. Initialize/apply bootstrap and configure the production S3 backend as the
   guide specifies. Plan, inspect, and apply saved production plans. If creating
   the Route 53 zone, establish registrar delegation before certificate issuance;
   reuse an existing zone when configured. Report any registrar action that
   cannot be completed through available access.
5. Render values with `uv run python scripts/render_aws_deployment_values.py`,
   seed `data/AB_NYC_2019.csv` in the Terraform output bucket if missing, and
   publish/promote immutable images using the existing workflow. Commit/push the
   reviewed rendered values and image promotion to the Git revision Argo CD
   reads. Do not invent image tags or use `latest`. Run
   `uv run python scripts/validate_aws_deployment.py --check-images`.
6. Configure kubeconfig using the Terraform `cluster_name` output. Install
   Argo CD and apply `serving/argocd/root-application.yaml` as documented. Manually
   sync root, controllers, monitoring, and platform; sync operations must include
   `CreateNamespace=true` and `ServerSideApply=true`. Keep automatic sync disabled.
   Wait for controllers before dependent applications, especially the AWS load
   balancer controller, cert-manager, and Traefik.
7. Trigger `airbnb_feature_build`, then `airbnb_model_training` through Airflow
   and wait for a governed MLflow `champion` before syncing inference. If retained
   snapshots/data are being restored, restore the databases, artifact data, and
   required runtime secrets before resuming workloads; an ordinary Terraform
   apply creates empty databases and does not restore the previous model registry.
   For an Airflow database restore, restore the backed-up `airflow-runtime`
   Secret before syncing platform, stripping its old cluster metadata. Set
   `platform.airflow.generateRuntimeSecret=false` to preserve those keys; enable
   generation only for a fresh database. Never commit the Secret data.
8. Verify Argo applications, pods, ExternalSecrets, PVCs, the inference
   certificate, `/ready`, a real `/predict` request, and Prometheus/Grafana
   collection. Run `load-tests/` as its README describes only after readiness.
   Report the endpoint, verification results, and any unresolved launch blocker.

Do not print credentials or Kubernetes Secret contents. Launch is complete only
when inference and monitoring work; a successful Terraform apply alone is not
enough.
