# Airbnb alert runbooks

| Alert | Severity | Runbook |
|---|---|---|
| `AirbnbDriftMonitoringStale` | warning | [Stale monitoring](drift-monitoring-stale.md) |
| `AirbnbFeatureDriftDetected` | warning | [Feature drift](feature-drift.md) |
| `AirbnbPredictionDriftDetected` | critical | [Prediction drift](prediction-drift.md) |
| `AirbnbModelQualityMonitoringStale` | warning | [Model quality](model-quality.md) |
| `AirbnbProductionOutcomesStale` | warning | [Model quality](model-quality.md) |
| `AirbnbProductionMAEDegraded` | critical | [Model quality](model-quality.md) |
| `AirbnbProductionPredictionBiasHigh` | warning | [Model quality](model-quality.md) |
| `AirbnbOutcomeMatchRatioLow` | warning | [Model quality](model-quality.md) |
| `AirbnbProductionDataQualityDegraded` | warning | [Data quality](data-quality.md) |
| `AirbnbProductionDataStale` | warning | [Data quality](data-quality.md) |
| `AirbnbServingErrorRateHigh` | critical | [Serving error rate](serving-error-rate.md) |
| `AirbnbPredictionLatencyP95High` | warning | [Prediction latency](prediction-latency.md) |

The primary owner is the ML Platform on-call. Product/Data Science owns model
quality decisions; SRE owns cluster and telemetry availability.
