# CI/CD refactor checkpoint

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

- Completed: Phase 0 (`acd0222`); Phase 1 (this milestone commit; resolve with
  `git log --oneline --grep='independent gated'`).
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
- Next: Phase 2 — persistent approved-model recovery into a fresh registry.
- Remaining: Phase 2 registry recovery; Phase 3 k6; Phase 4 dashboards;
  Phase 5 optional autoscaling proposal only if earlier phases complete.
- No deployments, AWS mutation, full training or node provisioning authorized.
- GitOps production applications require operator sync; a merged image PR alone
  does not imply a deployed or verified release.

## Resume

```sh
git status --short
git log -6 --oneline
cat docs/codex-checkpoint.md
.venv/bin/pytest tests/test_delivery_workflows.py tests/test_training_dag.py tests/test_feature_build_dag.py
```

Continuation prompt:

"Read docs/codex-checkpoint.md, inspect the current Git status and recent commits, and resume from the first incomplete phase. Do not redo completed work. Preserve existing changes. Follow the original sequential-delivery requirements and commit each tested milestone."
