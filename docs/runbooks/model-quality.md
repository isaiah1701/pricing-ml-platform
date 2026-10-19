# Production model quality

## Impact

Production error may have degraded, predictions may be systematically biased,
or delayed outcomes may no longer be arriving. Do not infer model quality from
prediction drift alone.

## Diagnosis

1. Confirm the quality DAG completed and the newest outcome is recent.
2. Check the overall matched sample count before interpreting error metrics.
3. Compare MAE, RMSE, bias, and tolerance ratios by concrete model version.
4. Inspect room-type, neighbourhood-group, and actual-price-band segments.
5. Check invalid rows and duplicate prediction IDs in the JSON report.
6. Verify actual outcomes use the same target definition as training labels.

## Mitigation

Restore outcome ingestion or repair prediction-to-outcome joins when telemetry
is stale or match coverage is low. For genuine degradation, pause promotion,
investigate drift and affected segments, and compare the champion with the
rollback version on the same labeled window.

## Rollback

Move the MLflow `champion` alias to the last verified version only after the
sample size and target definition have been validated. Confirm serving refresh
and continue monitoring the next labeled window.

## Ownership

Data Engineering owns outcome delivery and joins. Data Science owns quality
interpretation. ML Platform owns metric publication, promotion, and rollback.
