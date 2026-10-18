# Approved model recovery without retraining

The existing S3 platform bucket retains models independently of the EKS worker
and MLflow PostgreSQL database. A registry alias is database metadata: preserving
only the original MLflow S3 objects cannot reconstruct it.

`training.recovery` stores a self-contained, content-addressed bundle under
`s3://<existing-platform-bucket>/snapshots/approved-models`. It contains the
approved model's files, per-file checksums, aggregate artifact hash, original
version/run/source, code and dataset identities, parameters, evaluation metrics
and release-bundle provenance. An alias pointer is written only after all files
and the manifest have been uploaded. Repeated exports reuse identical objects.

New training registration and successful governed challenger promotion export
the current champion when `APPROVED_MODEL_STORE` is configured. The Helm Airflow
environment supplies the existing bucket prefix; its current pod identity already
allows read/write there. No new bucket, database or IAM role is needed. A failed
export fails the task and needs operator attention: do not tear down until a
verified export exists. A failed post-promotion check retains the previous backup;
the existing retraining DAG rolls the alias back to that previous model.

## Before the first teardown with this refactor

The currently running model predates this mechanism. After explicitly approving
and releasing the new training runtime, export it once from the existing Airflow
scheduler container (the current environment is named `airflow`):

```sh
kubectl -n airflow exec deployment/airflow -c scheduler -- \
  python -m training.recovery export
```

Then verify every retained byte independently of the source database:

```sh
kubectl -n airflow exec deployment/airflow -c scheduler -- \
  python -m training.recovery verify
```

The verification must report the same recovery bundle identity as export. It
checks the retained pointer/manifest, file paths, per-file and aggregate artifact
hashes, and release provenance without connecting to MLflow or modifying S3.
An existing champion is not a substitute for this check: restore deliberately
skips when champion already exists. Do not tear down until export and verification
both succeed. No one-time production export/verification was performed during
this task.

Export does not train or change the champion alias. It writes a retained S3 backup.
The source must be READY, have passed artifact reload verification, and have
valid, mutually consistent registry/release provenance. Do not manufacture
missing provenance or export an unevaluated model to bypass these checks.

## Restore after EKS or RDS recreation

1. Restore the retained bucket and existing identities in Terraform, then the
   databases, MLflow service and Airflow runtime. Prefer an RDS snapshot when
   available: it preserves the full registry and experiment history.
2. Verify the retained bundle with `python -m training.recovery verify`. Once
   MLflow is healthy, run:

   ```sh
   kubectl -n airflow exec deployment/airflow -c scheduler -- \
     python -m training.recovery restore
   ```

3. Restore validated offline features and materialize Redis using the data DAG
   if the online store was recreated. This operation is separate from training.
4. Sync inference through the existing explicit Argo gate. Check `/ready`,
   submit a valid prediction and verify the reported model version.

Recovery first checks whether champion exists. If it exists, it leaves it alone.
Registry errors are not interpreted as missing aliases. For a missing alias it
checks every checksum, safe relative path and provenance identity before any
registry mutation. It creates a recovery run and version, records original IDs,
rebinds the release bundle to the new run, then publishes the alias last. Existing
versions tagged with the recovery identity are reused after an interrupted restore.
Repeated successful restores create no extra runs or versions. No training path
is invoked. If no backup exists or integrity fails, recovery stops.

Version numbers in a fresh database can differ; `original_version`,
`original_run_id` and the original manifest retain traceability. This recovers
the approved model, not all historical experiments, Airflow metadata, secrets,
Feast registry or raw data. Keep those existing snapshot/backups too.

Run export/restore serially with promotion. MLflow does not offer a conditional
alias compare-and-swap; the tool checks again before publishing, but operators
must avoid racing a promotion in the final check/write interval. Checksums prove
integrity relative to the retained manifest, not authorship: protect S3 write
permissions, versioning and Governance Object Lock. Never bypass retention.

The shared artifact bucket now has Terraform `prevent_destroy`. A normal destroy
plan stops rather than deleting it. Retain the bucket/state and use the teardown
runbook's retained-resource procedure; removing this protection requires an
explicit reviewed persistence migration, not a routine teardown.

## Local verification

```sh
.venv/bin/pytest -q tests/test_model_recovery.py
```

Tests verify the bundle without opening a registry, then recover a tiny fixture
model into an empty temporary SQLite
registry and load it through the real inference loader. They also cover repeated
recovery, registry outages and corruption without AWS or production training.
S3 calls are tested using a mock client; no production restore has been performed.
