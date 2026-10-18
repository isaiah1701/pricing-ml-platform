# CI/CD refactor checkpoint

## Active brief: complete CI/CD and GitOps bootstrap

- Resume point: Phase A complete in this milestone commit (resolve with
  `git log --oneline --grep='finish independent ML and platform workflows'`).
- Exactly seven entry workflows: ML Data, ML Training, ML Deploy, Docker,
  Kubernetes, Terraform Plan and Terraform Apply. Removed k6 and consolidated
  general Python checks into Docker so Bootstrap will be the eighth workflow.
- Terraform handoff checks trusted run/commit/backend/provider lock/version,
  plan checksum/age, protected manual Apply and shared concurrency; no replanning.
- ML Deploy verifies/selects already approved versions via a manual Airflow DAG,
  preserves rollback, checks artifact bytes/provenance, and reuses Kubernetes.
  Initial gated registrations now carry promotion_status=approved. Docker runtime
  includes the model metadata modules required by the deployment DAG.
- Validation: **162 tests passed**, 14 dependency warnings; Ruff, actionlint and
  production Helm lint passed. Cloud paths mocked; no AWS mutation, actual model
  promotion, image build/push, deployment, full training or production load.
- Previously committed training checkpoint support: `d52c838`; verified native
  interrupted-fit resume plus completed-member reuse. S3 recovery remains
  independent of checkpoints/RDS snapshots (`8341b87`).
- Next: Phase B — inspect existing ownership, add manual Bootstrap with Helmfile
  and focused offline tests; then Phase C integration/remaining hardening.
- Prerequisites remain unverified: actual protected environment reviewers,
  role permissions/OIDC, private runner reachability, Airflow bearer token,
  existing Terraform backend/inputs, and one-time approved-model S3 export.
- Preserve unrelated `README.md`, `load-tests/`, `skills/`. Do not push or run
  live apply/bootstrap/deployment/autoscaling/model training/load tests.

## Follow-up: training checkpoints and workflow separation

- User requested resumable training, removal of the k6 workflow, exactly three ML
  entry points plus separate Docker/Kubernetes/Terraform Plan/Terraform Apply.
- Reference repository's four workflow files were read through GitHub's public
  API/raw URLs after browser access to the directory failed. Reuse its separation
  of concerns and exact-plan artifact handoff; keep explicit apply/deploy gates.
- Training checkpoint milestone: native per-member CatBoost snapshots, completed
  member reuse, ordered input/config/code/version identity, filesystem locking,
  atomic completed models/checksums, Airflow PVC path and ignored local storage.
- Targeted verification: 11 checkpoint/training tests passed, including actual
  interrupted native snapshot resume and prediction parity. No full ensemble run.
- Details/limits: `docs/training-checkpoints.md`. PVC resume is not S3 retention.
- Next: reorganize and validate workflows; preserve the pre-existing README,
  load-tests and skills changes. Do not deploy, apply or push.

## Baseline — Phase 0 complete

- Branch `main`, starting commit `3887009`.
- Pre-existing work preserved: modified `README.md`; untracked `load-tests/` and
  `skills/`. Do not include these in refactor commits.
- Independent manual Airflow DAGs already exist: `pipelines/feature-build.py`
  and `pipelines/training.py`. Training evaluates MAE and reloads artifacts before
  registry publication. `retraining/retrain.py` adds comparison, human approval,
  post-promotion checks and rollback for drift-driven challengers.
- Existing validation runs Python tests, two Docker builds, Helm/schema checks
  and Terraform checks for every matching change. Publishing is manual, gated
  by `AWS_PUBLISH_ENABLED` and the production environment, but builds both images.
  Deployment itself does not train; inference resolves MLflow's champion alias.
- Retained S3 objects preserve artifacts and dataset snapshots. RDS final
  snapshots preserve registry metadata. S3 alone currently cannot restore aliases
  into a fresh database. Production uses one budget worker; do not increase it.
- GitHub CLI repository discovery failed (no authenticated session); continued
  immediately with the current repository as instructed.
- Baseline inspection verified workflow gates and DAG ordering from source;
  live infrastructure was not changed or revalidated in this refactor.

## Delivery state

- Completed: Phase 0 (`acd0222`); Phase 1 (`e876ff1`); Phase 2 (`8341b87`);
  Phase 3 (`ecf4ff9`); Phase 4 (`43cb04d`). Phase 5 delivered only as the
  permitted tested proposal (`f56c760`).
- Phase 1 changes: added data/training entry workflows and a reusable approved
  Airflow executor; selective Docker validation; serving-only automatic publishing
  with manual training-image selection; cached builds/dependencies; ECR digests
  in GitOps promotions and Helm; checksum-verified preprocessing reuse; explicit
  pre-training drift approval; S3 publication of data snapshots.
- Validation: full pytest run had 121 passing tests and one new Helm test fixture
  failure (missing DB coordinates); fixed the fixture and reran the complete
  delivery-workflow module successfully (15 tests). Other 107 tests were unchanged
  by that fixture fix. Targeted DAG/execution/deployment suite: 35 passed before
  the added digest test. Repository Ruff, actionlint 1.7.7, two Helm lint variants,
  and `git diff --check` passed. No container push or production run performed.
- Relevant paths: `.github/workflows/{data,training,airflow-run,publish-images,validate}.yml`,
  `scripts/trigger_airflow.py`, `docs/pipelines.md`, preprocessing, retraining and
  Helm image helper changes; policy/execution tests.
- Limitation: GitHub execution requires a runner reachable HTTPS Airflow API,
  a valid bearer token, and `ML_PIPELINE_EXECUTION_ENABLED=true`. Configure required
  reviewers on the production environment. Current private Airflow is not exposed
  by this change. Existing manual DAG/UI entry points remain available.
- Phase 2 changes: `training/recovery.py` exports an approved alias plus all model
  files and provenance to the existing snapshots prefix. It restores verified
  artifacts into a fresh MLflow DB, rebinds run/release identities, preserves
  original IDs, publishes aliases last and reuses prepared recovery versions.
  Initial registration and successful challenger promotion persist champion when
  configured; Airflow Helm supplies the store. MLflow now uses the shared digest
  helper too. Artifact bucket has Terraform prevent_destroy. Recovery instructions
  and limitations: `docs/model-recovery.md`; tests: `tests/test_model_recovery.py`.
- Phase 2 validation: 8 recovery tests passed (real local registry and inference
  loader; corruption, interrupted retry, outage, mocked S3). Full suite: **130
  passed**, 14 dependency warnings. Ruff, actionlint, Helm lint, Terraform fmt and
  bootstrap/production Terraform validate passed. No AWS write or actual training.
- MLflow detail: use the exact logged-model URI recorded in training provenance
  for backup downloads; registry-version downloads generate different metadata.
- Production limitation: release the new runtime and explicitly export the
  existing champion before teardown. No backup was written to production during
  this task. Recovery must be serialized with promotion (no MLflow alias CAS).
  S3 checksums are not signatures; retain restricted writers/Object Lock.
- Phase 3 changes: `tests/k6/{smoke,baseline,burst,common}.js`, deterministic
  existing listing samples, configurable provisional thresholds, explicit remote
  opt-in, recovery-window metrics, local real-FastAPI fixture and instructions.
  `.github/workflows/k6.yml` runs one-user local smoke only.
- Phase 3 validation: actual k6 1.3.0 local runs all exited zero: 3-second smoke
  (3 predictions), shortened 100-VU baseline (300), shortened 10-VU burst (87)
  with recovery samples. All error rates zero; fixture p95s 8.72/542.36/18.47 ms,
  recovery p95 19.73 ms. These are script/transport checks, not production
  performance results. 3 payload-schema tests passed; actionlint and diff checks
  passed. Default 100-VU/5-minute baseline and 130-second burst options inspected.
  Remote-host rejection tested via `k6 inspect -e BASE_URL=https://platf0rm.site`
  (no request issued). Fixture server stopped.
- Phase 4 changes: namespace-scoped/deduplicated container CPU/memory with separate
  units, node utilisation, Airflow/MLflow replica/container readiness. Application
  alerts deduplicate stable/canary scrapes too. Preserved the two Helm-only serving
  availability/capacity alerts and added them to the local source alert file;
  source/chart alert rules now agree. Added metric-declaration/provisioning tests,
  rule generation and real promtool syntax/synthetic-series CI checks.
- Phase 4 validation: 22 targeted dashboard/workflow/canary tests passed; final
  alert-mirror adjustment passed the 3 dashboard tests again. Promtool 3.5.0
  validated 32 dashboard queries and 15 alerts; duplicate-scrape fixture passed
  (2 ready pods, 3 prediction requests/s; health traffic excluded). Ruff, actionlint,
  Helm lint and diff checks passed. Docker daemon unavailable locally; used the
  official release promtool binary. No production sync/live metrics check.
- Monitoring limitation: Airflow DAG-failure counters are not exported; inspect
  Airflow runs directly. MLflow readiness is a Kubernetes probe, not registry API
  validation. Drift/outcome data still depend on the existing batch jobs.
- Phase 5 proposal: `docs/proposals/autoscaling/` contains provider-free Terraform
  outputs for the NodePool/EC2NodeClass, a min=1/max=2 HPA overlay, policy tests,
  controller placement/IAM/bootstrap/disruption analysis and explicit blockers.
  Helm accepts HPA behavior and omits Rollout replicas when HPA owns scaling.
  The proposal is outside GitOps; production remains fixed at one worker/no HPA.
- Phase 5 validation: Terraform fmt/init/validate and its offline test passed;
  20 targeted HPA/canary/workflow tests and proposal Helm lint passed. Final
  full Python suite: **137 passed**, 14 dependency warnings; Ruff, actionlint and
  diff checks passed. No Docker builds, GitHub runs, AWS mutation or retraining.
- Remaining: actual Karpenter integration is NOT implemented. Controller IAM,
  worker profile/EKS access, pinned controller/CRDs, Argo replica ownership, live
  Metrics Server and capacity testing require a separate infrastructure change.
  Karpenter's eventually consistent limits do not prove the strict global
  two-node ceiling, including replacements. This blocks activation, not the
  completed offline proposal. See the proposal README's official references.
- Next: review/resolve the strict node budget and activation gates before any
  autoscaling implementation. Do not apply the proposal as if it were finished.
- User approval required for deployment, AWS infrastructure changes, production
  export/restore, full training or additional nodes. Nothing was pushed or deployed.
- Preserved final worktree changes: `README.md`, `load-tests/`, `skills/` from
  before this refactor. All refactor changes are committed by milestone.
- No deployments, AWS mutation, full training or node provisioning authorized.
- GitOps production applications require operator sync; a merged image PR alone
  does not imply a deployed or verified release.

## Resume

```sh
git status --short
git log -6 --oneline
cat docs/codex-checkpoint.md
.venv/bin/pytest tests/test_delivery_workflows.py tests/test_training_dag.py tests/test_feature_build_dag.py
.venv/bin/pytest -q tests/test_model_recovery.py
.venv/bin/pytest -q tests/test_k6_contract.py
.venv/bin/pytest -q tests/test_monitoring_dashboards.py
.venv/bin/pytest -q
terraform -chdir=docs/proposals/autoscaling init -backend=false
terraform -chdir=docs/proposals/autoscaling test
k6 run tests/k6/smoke.js  # start the local fixture first; see tests/k6/README.md
```

Continuation prompt:

"Read docs/codex-checkpoint.md, inspect the current Git status and recent commits, and resume from the first incomplete phase. Do not redo completed work. Preserve existing changes. Follow the original sequential-delivery requirements and commit each tested milestone."
