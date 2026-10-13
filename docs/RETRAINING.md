# Retraining, approval, promotion, and rollback

## Trigger and reproducibility

The monitoring DAG publishes the `airbnb_retraining_event` Airflow Asset only
from its threshold-passing branch. That Asset schedules the
`airbnb_model_retraining` DAG. The durable event contains the production data
path, window row count, drift report path, and model version that caused the
decision.

Retraining records those fields as MLflow parameters and in the evaluation
report alongside the dataset SHA-256, Git SHA, Feast feature service, and fixed
holdout seed. The current champion and challenger are evaluated against the
same held-out rows.

## Automated gates

A challenger proceeds only when all configured rules pass:

- MAE improves by at least `MINIMUM_MAE_IMPROVEMENT` dollars (default `2.0`);
- RMSE does not exceed champion RMSE by more than
  `MAXIMUM_RMSE_REGRESSION` (default `2%`); and
- entire-home, private-room, and ordinary-market MAE do not regress by more
  than `MAXIMUM_SEGMENT_MAE_REGRESSION` (default `5%`).

Failed challengers leave the production alias unchanged. Their MLflow run is
tagged `promotion_status=rejected` with machine-readable rejection reasons.
Passing challengers are registered and assigned only the `challenger` alias.

## Human approval

Airflow's standard-provider `ApprovalOperator` presents fixed Approve and Reject
options. Airflow records the selected option, responder, and response timestamp
and keeps an unanswered task in the native awaiting-input state. Promotion also
validates the approval payload and copies its identity and timestamp into MLflow
model-version tags.

Before approving, compare the identical-holdout report, segment guardrails,
dataset window, Git SHA, and challenger artifact-reload result in MLflow.

## Promotion and rollback

On approval, the workflow resolves the current `champion`, saves it under the
`rollback` alias, and then atomically points `champion` at the challenger. It
waits for serving to report the new concrete version through `/ready`, submits
20 prediction smoke requests, validates finite non-negative prices and response
version, and enforces `POST_PROMOTION_P95_SECONDS` (default `0.25`).

Health, version, prediction, or latency failure routes to
`rollback_promotion`, which restores the saved version and tags the failed
candidate `promotion_status=rolled_back`.

## Verify zero-deployment refresh

No code or image rollout is part of promotion. The existing serving refresh
loop follows the MLflow alias and atomically swaps the fully loaded artifact.
Verify it directly:

```sh
curl --fail http://localhost:8000/ready
curl --fail http://localhost:8000/metrics | grep airbnb_loaded_model_info
```

Both outputs must show the promoted concrete model version. During rollback,
they must return to the version saved by the workflow under the `rollback`
alias.
