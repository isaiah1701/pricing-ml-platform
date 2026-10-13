# AirbnbFeatureDriftDetected

## Impact

At least one feature PSI exceeds 0.2. The production population differs from
training and model accuracy may degrade, especially when several related
features drift together.

## Diagnosis

1. Open the newest timestamped Evidently HTML report.
2. Identify the `feature` and `kind` labels with the highest PSI scores.
3. Compare ingestion volume, null rates, schema, units, and category mappings.
4. Determine whether the change is genuine market behavior or a pipeline defect.
5. Check whether the monitoring DAG emitted a retraining event.

## Mitigation

Correct upstream transformations for data defects and rerun monitoring. For
genuine sustained drift, validate the production window, run retraining, and
compare the candidate against the existing champion before promotion.

## Rollback

Roll back the feature-producing deployment when it introduced the shift. If a
new model version is sensitive to the shifted feature, move the MLflow champion
alias to the last verified version and confirm serving refresh.

## Ownership

Data Science owns drift interpretation. Data Engineering owns feature defects;
ML Platform owns report generation and retraining-event delivery.
