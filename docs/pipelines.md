# Independent pipelines

| Entry point | Code-change behavior | Production execution |
| --- | --- | --- |
| `data.yml` | Validate preprocessing, lineage, Feast and DAG tests | Dispatch with `execute=true`: `airbnb_feature_build` |
| `training.yml` | Lightweight model/gate/DAG tests; no full ensemble | Dispatch with `execute=true`: `airbnb_model_training` |
| `publish-images.yml` | Serving runtime changes on main propose an image promotion | Environment approval, OIDC publish, scanned digest, reviewed GitOps PR and explicit Argo sync |

Documentation and infrastructure changes do not trigger these ML entry points.
`validate.yml` retains the complete test/manifests/Terraform suite; Docker builds
are selected by runtime inputs. Feature code does not build the serving image.
The serving image includes `training/train.py` for estimator deserialization, so
changes to that shared class intentionally validate/publish serving too.

Data execution validates/cleans, builds deterministic offline features, publishes
content-addressed lineage snapshots, applies Feast, materializes Redis, and
verifies offline/online consistency. Matching input, preprocessing source and
dependency versions reuse the checksum-verified Parquet output. Feast apply and
Redis materialization deliberately repeat so an emptied online store recovers.
Set `AIRBNB_SNAPSHOT_S3_URI` to retain dataset snapshots outside the worker.

Training loads validated features and uses existing MAE and artifact reload gates
before publishing `champion`. Drift events enter the existing retraining DAG,
which now requires native Airflow approval **before** expensive training as well
as approval before production promotion. New dataset ingestion is currently an
operator action: approve the dataset, run data, inspect its version/validation,
then explicitly request training. No new dataset event system is introduced.

## Configure GitHub execution

Production ML execution is disabled unless `ML_PIPELINE_EXECUTION_ENABLED=true`.
Set `AIRFLOW_API_URL` to an HTTPS endpoint reachable from the runner and provide
a valid Airflow 3 bearer token in the `AIRFLOW_API_TOKEN` secret. It must remain
valid through polling. Grant that identity access only to trigger/read the two
listed DAGs. `AIRFLOW_RUNNER_LABEL` can select an existing runner with private
network access. The current private Airflow service is **not** reachable from a
standard GitHub runner: do not enable execution until connectivity and TLS are
configured. Use the existing Airflow UI/manual entry points in the meantime.
No ingress, runner, credentials or AWS permissions are provisioned here.

Configure required reviewers on the existing `production` GitHub environment.
Dispatches execute only from main and wait for DAG success; timeouts do not cancel
a running DAG. Inspect Airflow before retrying. GitHub run IDs prevent ambiguous
duplicate submissions; max_active_runs=1 prevents concurrent executions per DAG.

Image publication retains `AWS_PUBLISH_ENABLED`, the production environment and
the existing ECR-only OIDC role. Manual `component=training` builds only the
Airflow/training runtime; serving is the default. The workflow never trains.
Shared training runtime changes must be published/promoted and explicitly synced
before executing the updated DAGs. Dependency and Docker caches reduce repeated
build work. Existing SHA tags remain supported; new promotion PRs include the
ECR digest and Helm uses `repository@sha256:...` when present.

## Release and model-only promotion

1. Review the image PR and CI; merge it only after approval.
2. Explicitly sync the existing Argo application. This workflow does not deploy.
3. Check Argo health and `/ready`, then submit a valid `/predict` request using
   the existing load-test fixtures. Record the observed model version.
4. For model-only changes, use the governed Airflow promotion path. Serving
   periodically resolves `champion`; no Docker rebuild is needed. Verify the
   observed version and inference. Existing post-promotion checks can roll back
   a failed model promotion.

The current refactor is validated offline; production execution and release
verification require operator approval and are not claimed as completed runs.
