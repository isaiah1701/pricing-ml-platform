# AirbnbPredictionDriftDetected

## Impact

The predicted-price PSI exceeds 0.2. Pricing output has materially shifted and
may affect customer expectations, marketplace conversion, or downstream
decisions even when input-feature drift is not widespread.

## Diagnosis

1. Compare prediction histograms in Grafana and the latest Evidently report.
2. Check the active model version and recent MLflow alias changes.
3. Compare feature drift, request mix, prediction volume, and invalid-request rate.
4. Sample sanitized predictions across room types and price bands.
5. Verify the reference prediction column and production window are comparable.

## Mitigation

Pause promotion, investigate the active model and input mix, and obtain Data
Science approval before accepting a genuine market shift. Trigger validated
retraining when the configured dataset threshold is also met.

## Rollback

Move the champion alias to the prior verified MLflow version. Confirm every pod
reports that version through `/ready` and `airbnb_loaded_model_info`; watch the
prediction distribution normalize.

## Ownership

Data Science is accountable for pricing behavior. ML Platform owns model
versioning and rollback; the product owner must be informed for material shifts.
