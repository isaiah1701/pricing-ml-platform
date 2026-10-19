# Resumable training

CatBoost ensemble training now saves native per-member snapshots and completed
member models. Repeating a training task with the same inputs resumes unfinished
iterations and skips already completed members. Evaluation and promotion still
run their normal gates; a checkpoint is not an approved production model.

Airflow stores checkpoints in `/opt/airflow/training-checkpoints` on its existing
PVC. Local training defaults to ignored `data/training-checkpoints/`. Set
`AIRBNB_TRAINING_CHECKPOINT_DIR` or the estimator's `checkpoint_dir` to select a
different persistent filesystem. An explicit empty value disables checkpointing.
Snapshots default to every 60 seconds; `snapshot_interval` configures that
interval on the estimator. An interruption before the first snapshot may require
repeating the member from its beginning.

Checkpoint identity covers ordered features and targets, column types, ensemble
seeds/iterations/hyperparameters, training source and dependency versions. Changed
inputs/configuration/code use a separate directory. A filesystem lock prevents
two processes writing the same checkpoint concurrently. Completed models use
atomic writes and checksum receipts; damaged checkpoints fail closed. Inspect
and remove only the affected identity directory if an intentional fresh run is
required. Do not delete it automatically on task failure.

After a failed Airflow run, retry/clear the training task using the same validated
dataset, code and parameters. The existing DAG execution/approval gates remain;
no extra production training is triggered by enabling checkpoint support. Local,
initial training and governed retraining share the same estimator implementation.

PVC checkpoints survive process/pod restarts while that volume exists. They are
not automatically copied to S3 and do not promise resume after the PVC is deleted
or during full infrastructure teardown. Approved models still use the separate
S3 recovery mechanism documented in `docs/model-recovery.md`. The new runtime and
Helm environment must be explicitly released before live training uses this code.

Verification:

```sh
.venv/bin/pytest -q tests/test_training_checkpoints.py tests/test_train.py
```

Tests interrupt a tiny fixture fit after a real native snapshot, verify that its
retry starts from a later iteration and matches uninterrupted predictions, and
check completed-member reuse, changed-input isolation, corruption and locking.
They do not train the production ensemble.
