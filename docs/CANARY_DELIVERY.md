# Canary delivery, CI/CD, and GitOps

## Selected mechanism

This repository uses Argo Rollouts with Traefik weighted traffic
routing. It is the direct progressive-delivery extension to the existing Argo
CD and EKS design:

- Argo CD installs and reconciles the Argo Rollouts controller and CRDs;
- the existing `airbnb-inference` Service is the stable service;
- `airbnb-inference-canary` selects only the new ReplicaSet during rollout;
- Argo Rollouts updates TraefikService routing weights without changing stable selectors;
- Prometheus analysis aborts bad revisions before stable is promoted.

AWS Load Balancer Controller provisions Traefik's public NLB. cert-manager issues
and renews the TLS Secret used by the Traefik IngressRoute. ExternalDNS publishes
the hostname from Traefik's LoadBalancer Service. Production values must replace
the ECR and platform placeholders before merge.

## Rollout policy

The production Rollout sends 5% of traffic to the candidate, pauses for two
minutes, and evaluates prediction error rate and p95 latency. Passing revisions
progress through 25%, 50%, and 100%, with repeat analysis at each intermediate
stage. Each metric has `failureLimit: 1`; a failed analysis aborts the Rollout,
leaves the stable Service on the prior ReplicaSet, and removes canary traffic.

Default gates are:

- prediction HTTP 5xx ratio at or below 5%; and
- prediction p95 latency at or below 250 ms.

The thresholds, analysis interval/count, initial weight, and Prometheus address
are Helm values. Readiness probes prevent unready canary pods from receiving
traffic before analysis.

## Continuous validation

The validation is split by responsibility:

- `docker.yml` runs Python lint/regression checks and selectively builds runtimes.
- `kubernetes.yml` lints/renders Helm and validates schemas before an optional manual sync.
- `terraform-plan.yml` performs backend-free checks and optionally creates a reviewed saved plan.
- `terraform-apply.yml` consumes that plan only after manual dispatch and environment approval.

## Immutable image publishing and GitOps update

`docker.yml` uses the production environment and the existing ECR-only OIDC role
configured in the `AWS_ECR_PUBLISH_ROLE_ARN` variable. It scans selected/changed
runtimes, publishes immutable SHA tags and proposes ECR digests in production Helm
values. It does not call kubectl or deploy as a side effect. Required reviewers
and branch restrictions must be configured on the `production` environment.

Review/merge the image PR, then manually run Kubernetes (or ML Deploy for model
selection plus inference reconciliation). Applications retain explicit sync gates.
The Kubernetes workflow uses its separately configured EKS role and a reachable
runner to sync the reviewed commit through Argo, not a competing Helm release.
For complete variables and gate behavior, see `docs/pipelines.md`.

## Verification

Automated rendered-manifest tests assert that:

- canary traffic starts at 5%;
- stable and canary are distinct Services managed by Rollouts;
- error and latency failures terminate analysis after one failed measurement;
- the stable Service remains independently targetable during abort; and
- the passing path includes an explicit final 100% traffic step.

In a cluster, verify with `kubectl argo rollouts get rollout airbnb-inference
--watch` from an operator workstation. Only explicitly approved deployment jobs receive their configured EKS permissions.
