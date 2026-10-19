# Platform pipelines

| Name | Workflow | Responsibility |
| --- | --- | --- |
| ML Data | `ml-data.yml` | Schema/preprocessing/Feast tests; explicit data DAG execution |
| ML Training | `ml-training.yml` | Lightweight training, gate and checkpoint tests; explicitly approved training |
| ML Deploy | `ml-deploy.yml` | Verify/select an approved registry version and invoke Kubernetes reconciliation |
| Docker | `docker.yml` | Python regression checks; selectively build, scan and publish runtimes |
| Terraform Plan | `terraform-plan.yml` | Backend-free validation; optionally produce a saved production plan |
| Terraform Apply | `terraform-apply.yml` | Manually apply an approved saved plan |
| Bootstrap | `bootstrap.yml` | Manually establish the existing Argo CD release and root Application |
| Kubernetes | `kubernetes.yml` | Helm/schema validation and explicitly approved Argo sync |

These eight workflows are independent entry points;
none automatically runs every downstream workflow. k6 remains local-only under
`tests/k6/`; it has no GitHub Actions workflow. There is no extra CI workflow or
internal runner workflow: shared Python helpers keep the entry points small.

The layout follows the separation in the
[reference workflows](https://github.com/isaiah1701/Observability-fintech-platform/tree/main/.github/workflows).
Unlike the reference's automatic Apply chain, production application and
infrastructure changes require explicit manual dispatch and environment approval.

## ML execution

Relevant feature changes trigger ML Data validation. Relevant training changes
trigger ML Training validation, including checkpoint tests; neither code pushes
nor ordinary pull requests execute the production ensemble. Training publication
still requires existing MAE and artifact-reload gates. Drift-driven training and
promotion retain native Airflow approvals. Training checkpoints use the existing
Airflow PVC; see `docs/training-checkpoints.md` for scope and limits.

Configure `ML_PIPELINE_EXECUTION_ENABLED=true`, a runner label in
`AIRFLOW_RUNNER_LABEL`, a reachable HTTPS `AIRFLOW_API_URL` and a valid
`AIRFLOW_API_TOKEN` secret. Limit the API identity to trigger/read the three DAGs:
`airbnb_feature_build`, `airbnb_model_training`, `airbnb_model_deploy`. Tokens must
remain valid through polling. Private Airflow is not reachable from ordinary
GitHub-hosted runners without explicitly configured connectivity. No networking,
runner or credentials are provisioned by these workflows.

Data preprocessing reuses unchanged, checksum-verified processed inputs, then
reapplies Feast/materializes Redis to recover emptied online stores. An approved
new dataset remains an explicit operator handoff from data to training.

ML Deploy accepts an explicit previously approved version; a blank value retains
and verifies the current champion. It checks READY status, source run/reload
verification, serving's provenance gates and the actual model artifact checksum.
A non-current version must already have `promotion_status=approved`; awaiting
approval/rejected challengers cannot bypass the existing retraining process.
Initial training marks successful gated registrations approved. The old current
champion remains supported even if it predates that tag. Promotion preserves the
old champion as rollback and exports the configured S3 recovery bundle.

Publish and sync the updated training/Airflow runtime before executing updated
DAGs. ML Deploy does not build images or run Terraform. Avoid concurrent registry
promotions; MLflow offers no atomic conditional alias update. Existing governed
retraining post-promotion checks and rollback remain available.

## Docker and Kubernetes

Docker runs Python regression tests and builds only changed runtime inputs.
Manual dispatch selects exactly one runtime. PRs never assume the publish role.
`AWS_PUBLISH_ENABLED=true`, the `production` environment and
`AWS_ECR_PUBLISH_ROLE_ARN` variable gate ECR publication through OIDC. Images are
scanned for HIGH/CRITICAL findings before publication; promoted values contain
immutable digests. Publishing only proposes a GitOps PR; it never deploys.

Review/merge the image PR, then manually run ML Deploy for inference or Kubernetes
for platform changes. Kubernetes requires `deploy=true`, main,
`K8S_DEPLOY_ENABLED=true` and the protected `production` environment. Set
`AWS_EKS_DEPLOY_ROLE_ARN`, `EKS_CLUSTER_NAME`, `AWS_REGION`, an existing reachable
runner through `EKS_RUNNER_LABEL`, and an EKS-compatible `KUBECTL_VERSION` (default
v1.35.0 matches this repository). Give the role only the required EKS access and
Argo application read/patch/wait permissions, ECR DescribeImages for legacy SHA tags,
and read access to the inference Rollout/pod status. No role is created here.

Select `controllers` to sync the reviewed root and then its declared controller
Applications in dependency-wave order; select `airbnb-platform` or
`airbnb-inference` separately. Controller sources must exactly match the reviewed
root chart and use their pinned chart versions. Git applications use the reviewed
Git SHA. Each sync request has a unique operation marker, so an old success at
the same revision cannot satisfy a new request. Reconciliation patches only the
selected existing Argo Application's sync operation. It validates repository/path/branch and
waits for that operation and revision to be Synced/Healthy, rather than accepting
an old healthy status. It does not create competing Helm releases. For inference it compares the live
Rollout image with reviewed values and checks ready desired pods against the ECR
digest. Legacy immutable SHA tags are resolved read-only; no image build is implied.
ML Deploy passes its selected model version into Kubernetes. Bounded readiness/
inference polling allows the existing champion-refresh interval after sync; `SERVING_URL` defaults to
`https://platf0rm.site`. Model-only promotions need no image rebuild.

## Reviewed Terraform plans

PRs only run format/backend-free validation, including bootstrap/prod Terraform
and the isolated autoscaling proposal. A real plan is allowed only on main with
`TF_PLAN_ENABLED=true` and environment approval. Configure:

- `AWS_TF_PLAN_ROLE_ARN`: a reviewed plan role with required AWS read access and
  existing remote-state locking access.
- `TF_STATE_BUCKET`, `TF_STATE_KEY`, `AWS_REGION`: the actual existing backend.
- `AWS_ACCOUNT_ID`: the independently configured expected production account;
  STS must match for both Plan and Apply.
- `TF_PROD_TFVARS_JSON` secret: the actual production inputs as JSON, explicitly
  including `cost_optimized=true`. CI cannot silently fall back to larger defaults.

The artifact contains the binary plan, identity metadata and value-free change
addresses/actions. It excludes tfvars and state. Metadata binds repository,
commit, workflow run, production environment/AWS account, backend fingerprint,
Terraform version and provider lock
hash. Plan contents are sensitive: retain private repository artifact access;
retention is one day. Failed Terraform output is kept only in owner-readable
runner files, not published as an artifact.

Apply is manual-only: provide `plan_run_id`, the reviewed `source_sha`, and
`confirm_apply=true`. It also requires `TF_APPLY_ENABLED=true`, main, protected
`production`, and `AWS_TF_APPLY_ROLE_ARN`. GitHub API checks require a successful
trusted Terraform Plan run on main, not a PR/fork artifact. The downloaded binary
and metadata are checked against the approved run, commit, backend, provider
lock and version; plans older than 24 hours are rejected. Missing or mismatched
artifacts fail. A plan is also rejected if main has advanced since it was made;
request/review a new plan explicitly in that case. Apply never replans. Terraform's native saved-plan state serial
check rejects plans made stale by another apply. Plan and Apply share a production
concurrency group and use the existing S3 state lock.

Use separate least-privilege AWS roles where available and configure required
reviewers/branch restrictions on `production`. Workflows declare environment
gates; reviewers must actually be configured in GitHub. Existing S3 model bucket
protection remains in effect. No workflow applies on a normal push.

## Offline validation

```sh
.venv/bin/pytest -q tests/test_delivery_workflows.py tests/test_platform_pipelines.py tests/test_model_deployment.py
.venv/bin/pytest -q tests/test_training_checkpoints.py tests/test_model_recovery.py
uvx --from ruff==0.16.7 ruff check .
actionlint -shellcheck=
```

Cloud-dependent paths are exercised with mocks/local registries. No live AWS,
GitOps sync, model promotion, production load test or Terraform apply was run.

For manual control-plane establishment, ownership and prerequisite checks, see
`docs/bootstrap.md`. Bootstrap never runs for ordinary application changes.
