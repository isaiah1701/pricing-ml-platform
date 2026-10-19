# Production data quality

## Impact

Missing, non-finite, or unseen feature values can make drift reports unreliable
and may cause prediction failures or silent quality degradation.

## Diagnosis

1. Identify features exceeding missing or unseen-category thresholds.
2. Compare the production schema and transformations with the training snapshot.
3. Check ingestion volume, source freshness, units, and category mappings.
4. Inspect the latest drift JSON report for valid and invalid row counts.

## Mitigation

Quarantine malformed rows, restore the expected transformation, and regenerate
the monitoring window. Accept a genuinely new category only after schema and
model compatibility are reviewed.

## Rollback

Roll back the feature-producing deployment when it introduced the violation.
Do not promote a new model using a corrupted monitoring or training window.

## Ownership

Data Engineering owns source and transformation quality. ML Platform owns
validation and telemetry. Data Science approves intentional schema evolution.
