# Data validation, immutable snapshots, and lineage

The feature-build, training, and retraining paths preserve the exact validated
dataset used by each run. A snapshot is addressed by the SHA-256 digest of its
Parquet bytes:

```text
data/snapshots/<dataset-sha256>/listing_features.parquet
data/snapshots/<dataset-sha256>/lineage/<lineage-id>.json
```

In AWS, Airflow also publishes both files under
`s3://<platform-data-bucket>/snapshots/<dataset-sha256>/` using conditional,
create-only writes. The production bucket has versioning and default Governance
Object Lock enabled, and the Airflow role has no permission to bypass retention,
so a published snapshot cannot be altered or deleted during its retention period.

In Docker Compose, snapshots live under `/opt/airflow/data-snapshots` on the
persistent `airflow-data` volume. Set `AIRBNB_SNAPSHOT_ROOT` to choose a
different durable filesystem location.

## Publication contract

Publication occurs only after all blocking rules in `training.validation` pass.
The snapshot writer then:

1. computes the complete file SHA-256;
2. copies the file to a temporary file and verifies the copied bytes;
3. creates the content-addressed destination without replacement;
4. marks the dataset and manifest read-only; and
5. verifies the snapshot again before Feast publication or training.

A changed dataset receives a different directory. Re-running the pipeline can
reuse identical snapshot bytes, but it never replaces them. Filesystem
permissions protect against accidental writes by the application. For
compliance-grade WORM retention, place `AIRBNB_SNAPSHOT_ROOT` on storage that
enforces retention independently of the process, such as an Object-Lock-enabled
S3 mount.

## Lineage manifest

Each write-once manifest records:

- dataset snapshot ID, path, hash, size, row count, columns, and schema hash;
- raw upstream path, hash, and size;
- transformation entry point and Git commit;
- the complete blocking validation report; and
- the Feast feature-service contract.

The feature-build DAG passes the immutable path to Feast. The training DAG
freezes its input before retrieval and checks that the mutable Feast source did
not change during retrieval. Both baseline and candidate MLflow runs store the
lineage ID as a tag, upload the manifest under `lineage/`, and upload the exact
Parquet snapshot under `data/`.

## Training attempts and release bundles

Every supported training entry point creates a parent MLflow attempt before
validation. Validation, retrieval, fitting, or logging failures terminate that
attempt with `FAILED`, while rejected and registered candidates finish with an
explicit outcome. Airflow remains the detailed task-level execution record.

Every successful model run also uploads a deterministic source archive under
`code/`. The archive includes tracked and non-ignored Python, configuration,
infrastructure, and dependency-lock files, so uncommitted and untracked source
changes alter `code_sha256`. The run records both the Git commit and whether the
worktree was dirty. Container images without `.git` fall back to hashing the
deployed source tree and set `git_state_available=false`; in that case the code
SHA-256, rather than a guessed commit, is authoritative.

After MLflow persists the model, the pipeline hashes the complete downloaded
model artifact and creates `provenance/release_bundle.json`. Its
`release_bundle_id` binds:

- parent training-attempt ID and model-run ID;
- exact code archive SHA-256 and Git state;
- immutable dataset SHA-256 and lineage ID;
- model artifact SHA-256 and URI; and
- Feast feature-service version.

Registered model versions copy the bundle ID and component hashes into Registry
tags before either `champion` or `challenger` is moved. If any metadata write
fails, the alias remains unchanged. This connects:

```text
raw dataset -> transformation commit -> validated snapshot -> Feast service
            -> MLflow run -> registered model version
```

To inspect an individual run, open its MLflow artifacts and download the JSON
file under `lineage/`. Its `dataset.sha256` must match the run's
`dataset_sha256` parameter. Then inspect `provenance/release_bundle.json`; its
bundle ID must match the `release_bundle_id` tag on both the run and any
registered model version.

Serving treats this provenance as a deployment gate. Before loading a concrete
Registry version it requires all bundle tags, checks the Registry run ID, loads
`provenance/release_bundle.json` from that run, recomputes its bundle ID, and
compares every code, data, model, and training-attempt identity with the version
tags. Missing or tampered provenance raises `ModelLoadError`; refresh failures
retain the currently loaded model.
