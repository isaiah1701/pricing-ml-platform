# Model and application recovery

These paths are implemented and tested offline. They have not promoted a live model,
failed a live canary, changed AWS, or proved live traffic restoration. Infrastructure
rebuild and model rollback are separate operations; rollback never runs Terraform.

## Model promotion failure

**Symptoms:** ML Deploy or retraining promotion fails, an approved model never becomes
ready, or the Model Quality & Drift dashboard shows no completed promotion. Inspect
`airbnb_model_promotions_total`, `airbnb_model_last_promotion_timestamp_seconds`,
`airbnb_model_rollbacks_total` and `airbnb_model_rollback_failures_total` in Prometheus.
The Model Inference dashboard compares the loaded version with the approved champion.

1. Open the failed Airflow task and its human approval record. Inspect MLflow version
   tags and source run status; do not approve a different model under an existing run.
2. Check the shared PVC operation receipt under `AIRBNB_PROMOTION_STATE_DIR` (production:
   `/opt/airflow/training-checkpoints/promotions`). It records the previous/promoted
   versions, approval, operation ID, deadline, state and rollback reason/timestamps.
3. A challenger and its previous champion must both pass run/provenance/artifact
   checksums before mutation. Correct missing/corrupt artifacts from retained verified
   storage rather than disabling verification. An approval compared against an older
   champion is rejected: evaluate and approve again.
4. Retraining routes failed serving checks to rollback. ML Deploy checks and rolls back
   within its selection task. Retry an interrupted task using the **same Airflow run**;
   the operation ID reuses its original previous champion. Never clear/delete the
   receipt merely to permit another promotion.
5. Verify `/ready` and `/predict` return the intended concrete version, a finite,
   nonnegative price, and successful predictions within the configured p95 guardrail.
   Only successful checks plus retained backup publication complete a promotion.

The lease is 15 minutes and new writers are blocked until completion or verified
rollback. Expiration does **not** allow takeover: use the saved receipt to roll back.
Task mutation locks wait at most 10 seconds; artifact I/O uses the existing MLflow
client timeouts. Never run promotion workers against separate local receipt directories.

## Model rollback failure

**Symptoms:** `airbnb_model_rollback_failures_total` increases, the Airflow rollback task
fails, or champion points back to the old version but serving still reports the new
one. Use Model Inference and Model Quality & Drift dashboards. Relevant queries:

```promql
airbnb_model_rollback_failures_total
airbnb_model_last_rollback_timestamp_seconds
airbnb_readiness
```

Read-only diagnostics (replace the namespace if configured differently):

```sh
kubectl -n airbnb-inference get pods,rollout
kubectl -n airbnb-inference logs -l app.kubernetes.io/instance=airbnb-inference --tail=100
kubectl -n mlflow get pods
curl --fail "$SERVING_URL/ready"
```

Restore MLflow/S3 connectivity or serving capacity first. A failed alias write or
failed restored-model verification retains a blocked receipt. Retry the original
rollback task; it verifies the previous artifact, restores only if the alias is
still this operation's candidate/previous version, and verifies serving again.

For an expired/interrupted ML Deploy run that cannot re-enter the promotion step,
an operator can recover **its reviewed saved receipt** inside the existing training
runtime, using existing MLflow credentials and the shared PVC:

```python
import json
import os
from pathlib import Path
from mlflow import MlflowClient
from training.deployment import rollback_promotion_verified, verify_serving_version

# Set to the inspected receipt path, never an unreviewed downloaded document.
receipt = json.loads(Path(os.environ["REVIEWED_PROMOTION_RECEIPT"]).read_text())
rollback_promotion_verified(
    MlflowClient(tracking_uri=os.environ["MLFLOW_TRACKING_URI"]), receipt,
    reason="operator recovery of interrupted promotion",
    verifier=verify_serving_version,
)
```

This is a production mutation requiring incident authorization. A different/newer
champion causes refusal, not an override. A rollback completes only after successful
previous-version predictions and backup persistence, then records `rolled_back`,
`rollback_verified_at`, and a deduplicated metric event. Telemetry publication failure
retains a pending event in the receipt and does not undo a verified model outcome;
retry the same completion/rollback to flush it. Unfinished receipts need review after
PVC loss; do not reconstruct them from whichever alias happens to be current.

The lock coordinates repository promotion/deployment paths on the **same shared
filesystem**. MLflow has no alias compare-and-swap API: it cannot prevent a privileged
external writer racing between an alias read and write. Restrict registry mutation
access to these workflows. Endpoint smoke checks sample the serving service; they do
not enumerate every replica. Native Argo application rollback does not undo model
aliases: the two controls are independent.

## Canary rollout failure

**Symptoms:** Kubernetes & Delivery shows a degraded Rollout or Failed/Error analysis,
new image remains unpromoted, or CanaryRolloutFailure fires. Inspect:

```promql
mlops_rollout_info{phase="Degraded"}
mlops_analysis_run_info{phase=~"Failed|Error|Inconclusive"}
```

```sh
kubectl -n airbnb-inference get rollout airbnb-inference -o yaml
kubectl -n airbnb-inference get analysisruns
kubectl -n airbnb-inference get replicasets,pods,services,traefikservices
kubectl -n argocd get application airbnb-inference -o yaml
```

Progression is 5 → 25 → 50 → 100 percent, with error and latency checks before each
increase. Each measurement needs at least 20 canary requests in two minutes. Empty
results, NaN, insufficient traffic or thresholds exceeded fail closed; no traffic is
never evidence that a revision is safe. Prometheus errors also cannot pass analysis.
Do not run production load generation or bypass failed analysis without approval.

Argo controls stable/canary selectors and Traefik weights. The stable ReplicaSet stays
scaled during analysis (`dynamicStableScale: false`), three ReplicaSet revisions are
retained, abort waits 30 seconds before scaling down the canary, and stalled progress
aborts after 600 seconds excluding pauses. These settings follow the
[Argo Rollout specification](https://argo-rollouts.readthedocs.io/en/stable/features/specification/).
The one-replica budget target temporarily needs capacity for both stable and canary.

If analysis fails, confirm the stable Service selector still targets `status.stableRS`,
Traefik weights restore stable=100/canary=0, stable endpoints are Ready, and readiness
plus approved-version predictions pass. Offline rendering tests validate configuration;
these controller/traffic checks remain a live acceptance gate.

Revert the **failed image publication commit** in Git, preserve the known good immutable
image digest, review/merge the revert, then run Kubernetes for `inference`. Do not
rewrite unrelated configuration or let Git keep declaring the failed revision. Argo
CD remains manual-sync by default. The inference Application ignores only the
Rollouts-owned Service pod-hash selectors and TraefikService traffic weights during
reconciliation; image references and other application fields remain managed by Git. A controller abort is not a durable Git revert;
avoid retrying or promoting the bad revision before Git is corrected. The existing
Kubernetes workflow verifies the reviewed reference and actual pod image digest.

## MLflow registry recovery

**Symptoms:** MLflow registry health is down or a recreated registry has no approved
champion. Use Pipeline Health and Model Inference dashboards; inspect
`airbnb_readiness` and the operational registry health series documented in the
[metric contract](metric-contract.md).

Follow [model recovery](../model-recovery.md): verify the approved S3 bundle, restore
it into the fresh registry, then verify concrete-version serving and predictions.
Neither restore nor verify starts training or needs the original RDS snapshot. The
read-only `python -m training.recovery verify` validates retained bytes without any
registry. Do not use an old current-champion shortcut as evidence that a backup is
intact. Keep locked S3 data and retention policies. The first production export and
verification are still a separate authorized live gate if not already recorded.

The recovery tests exercise real local MLflow registries, corrupt artifacts and
interrupted retries. Reviewed Terraform plan/apply guards and Bootstrap idempotency
remain covered by their existing offline tests; recovery never weakens those checks.
