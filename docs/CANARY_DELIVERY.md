# Canary delivery, CI/CD, and GitOps

## Selected mechanism

This repository uses Argo Rollouts with AWS Application Load Balancer traffic
routing. It is the direct progressive-delivery extension to the existing Argo
CD and EKS design:

- Argo CD installs and reconciles the Argo Rollouts controller and CRDs;
- the existing `airbnb-inference` Service is the stable service;
- `airbnb-inference-canary` selects only the new ReplicaSet during rollout;
- Argo Rollouts updates ALB routing weights without changing stable selectors;
- Prometheus analysis aborts bad revisions before stable is promoted.

The cluster must have AWS Load Balancer Controller installed with permissions to
manage the ALB. Production values must replace the ECR, ACM certificate, and DNS
placeholders before merge.

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

`.github/workflows/validate.yml` performs only read-only validation:

1. installs locked Python dependencies, lints every Python file, and runs all tests;
2. builds the serving image without pushing it;
3. lints default, base, production, and bootstrap Helm configurations;
4. renders workload/bootstrap manifests and validates them with kubeconform; and
5. runs `terraform fmt -check`, backend-disabled `terraform init`, and
   `terraform validate` for dev, staging, and prod.

The workflow contains no Terraform plan/apply and does not mutate infrastructure.

## Immutable image publishing

`.github/workflows/publish-images.yml` uses GitHub OIDC to assume the
least-privilege role stored in `AWS_ECR_PUBLISH_ROLE_ARN`. It builds serving and
training images, blocks on HIGH/CRITICAL Trivy findings, and pushes only full
`sha-<git-sha>` tags to the immutable ECR repositories.

Configure these repository settings:

- GitHub variable `AWS_REGION`;
- GitHub secret `AWS_ECR_PUBLISH_ROLE_ARN`; and
- a `production` GitHub Environment with required reviewers and deployment
  branch restrictions.

The GitHub Environment approval protects image publication and the GitOps
proposal. This is intentionally separate from the Airflow/MLflow human approval
that authorizes registry model promotion; each system retains its own audit trail.

## GitOps update

After publishing, CI changes only `serving/helm/values/production.yaml`, updating
both repositories and immutable tags. It opens a pull request and never invokes
`kubectl`. Review and merge of that pull request is the deployment authorization;
Argo CD observes Git and reconciles the Rollout.

## Verification

Automated rendered-manifest tests assert that:

- canary traffic starts at 5%;
- stable and canary are distinct Services managed by Rollouts;
- error and latency failures terminate analysis after one failed measurement;
- the stable Service remains independently targetable during abort; and
- the passing path includes an explicit final 100% traffic step.

In a cluster, verify with `kubectl argo rollouts get rollout airbnb-inference
--watch` from an operator workstation. CI does not receive Kubernetes credentials.
