# AirbnbPredictionLatencyP95High

## Impact

Prediction p95 exceeds 250 ms for ten minutes. Interactive callers experience
slow pricing responses and may time out or retry.

## Diagnosis

1. Compare p50, p95, and p99 against request rate, CPU, memory, and restarts.
2. Split observations by active model version and deployment revision.
3. Check for CPU throttling, garbage collection, noisy neighbors, and replica saturation.
4. Run the documented serving load test on the active artifact.
5. Inspect whether a recently promoted model increased inference cost.

## Mitigation

Scale replicas, correct resource limits, or remove unhealthy pods. Optimize only
after profiling confirms the bottleneck.

## Rollback

Restore the prior serving image or MLflow champion version if latency followed a
release. Verify p95 remains below threshold for at least ten minutes.

## Ownership

SRE owns capacity and Kubernetes performance. ML Platform owns serving code;
Data Science owns model-complexity regressions.
