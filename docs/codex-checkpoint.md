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

- Completed: Phase 0 (inspection and recoverable checkpoint).
- Next: Phase 1 — selective independent data/training/deployment entry points.
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
