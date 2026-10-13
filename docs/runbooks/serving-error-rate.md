# AirbnbServingErrorRateHigh

## Impact

More than 5% of prediction requests return HTTP 5xx for ten minutes. Clients
receive no price estimate and may retry, increasing load.

## Diagnosis

1. Check `/health`, `/ready`, request status metrics, and application logs.
2. Split errors by pod and active model version.
3. Inspect prediction-failure counts and recent model alias changes.
4. Check CPU, memory, restarts, MLflow availability, and malformed model output.
5. Reproduce one sanitized request against the affected version.

## Mitigation

Stop traffic to unready pods, scale healthy replicas if saturated, and correct
dependency or artifact access failures. Avoid logging request feature payloads.

## Rollback

Move the champion alias to the last verified version or roll back the serving
deployment. Confirm readiness and error ratio recover before restoring traffic.

## Ownership

ML Platform on-call leads. SRE owns cluster/network faults; Data Science owns
model-output failures.
