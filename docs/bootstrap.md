# GitOps bootstrap

The manual **Bootstrap** workflow establishes the control plane on an existing
Terraform-created production EKS cluster. It does not create AWS infrastructure,
add workers, train a model or run a downstream application deployment.

## Ownership

| Owner | Resources |
| --- | --- |
| Terraform | EKS/node group, VPC CNI, CoreDNS, kube-proxy, Pod Identity agent, EBS CSI and AWS services |
| Helmfile | Existing `argocd` release in namespace `argocd`, using `serving/argocd/values-budget.yaml` |
| Bootstrap | Root Application `prod-mlops-bootstrap`, from `serving/argocd/root-application.yaml` |
| Argo CD | AppProject and downstream controllers, monitoring, secrets, MLflow, Airflow and inference |

There was no existing Helmfile. `helmfile.yaml` codifies exactly the release name,
chart version and values already used by the production launch guide. It does not
install cert-manager, Traefik, External Secrets, monitoring or other Argo-owned
releases. Kubernetes workflows request Argo sync operations rather than managing
competing Helm releases.

## Prerequisites and manual execution

Configure required reviewers and main-branch restrictions on the `production`
GitHub Environment. Set repository variables:

- `BOOTSTRAP_ENABLED=true` only when an explicitly approved bootstrap is intended.
- `AWS_BOOTSTRAP_ROLE_ARN`: the reviewed OIDC role authorized for this repository
  and environment. AWS access only needs identity/EKS discovery; Kubernetes access
  must allow the Argo release/CRDs/RBAC, namespace and root Application management.
- `AWS_ACCOUNT_ID`: the expected 12-digit account ID, configured independently
  from the caller's current credentials.
- `AWS_REGION=eu-west-2`, `EKS_CLUSTER_NAME=prod-mlops-prod`.
- `EKS_RUNNER_LABEL`: an existing Linux amd64 runner with access to the restricted
  EKS API. The default hosted runner is usable only if that access already exists.
- `KUBECTL_VERSION`: an EKS-compatible version; the default v1.35.0 matches this
  repository's cluster minor. AWS CLI must be available on the runner.

Manually dispatch Bootstrap from main, choose `production`, and set
`confirm_bootstrap=true`. No other environment is inferred or provisioned.
`upgrade_argocd` defaults false: healthy existing controllers are preserved even
when the desired chart version differs. An explicit upgrade is a separate reviewed
decision. An unhealthy release at the same pinned version can be repaired;
unhealthy version changes require that upgrade input. Existing controllers with
unknown Helm ownership stop for migration review instead of being overwritten.

The workflow installs pinned Helmfile 1.5.0 with its release checksum, Helm and a
YAML reader. It authenticates via OIDC, verifies STS account/EKS ARN/region/name
and ACTIVE status, configures an isolated kubeconfig and checks the actual server.
It requires one or two existing workers and a Ready, schedulable `workload=system`
node. It waits for Terraform-managed foundational add-ons, validates Helmfile and
root references, installs/repairs Argo if needed, waits for its workloads, applies
the root with server-side ownership, and waits for the reviewed Git revision.

Helm and Kubernetes operations have timeouts; failure diagnostics report workload
readiness counts without container logs or Secret data. The workflow reports
registered downstream applications and pending manual reconciliations. A successful
bootstrap means the GitOps control plane/root can reconcile; it does not claim
that all platform services or inference are already restored. No destructive
Helmfile operation, forced ownership takeover or node scaling is used.

## Recovery handoff

1. Review and apply the exact saved Terraform plan through Terraform Apply.
2. Render/review the new non-secret Terraform coordinates into the existing
   production values and merge them at the Git revision Argo reads. Confirm
   immutable images already exist; Docker publishing is a separate workflow.
3. Explicitly run Bootstrap when control-plane recovery is needed.
4. Use Kubernetes to reconcile the Argo-owned foundational controllers, then
   platform services. Respect their existing dependency waves and CRD readiness.
5. Restore champion from the approved S3 bundle through `training.recovery` once
   MLflow and the Airflow runtime are healthy. Do not substitute retraining.
6. Materialize validated features into Redis if it was recreated, then run
   ML Deploy/Kubernetes for inference and verify the selected model.

The registry recovery can create metadata in a fresh database and does not need
the original RDS snapshot. Full Airflow database recovery still has its existing
encryption-key/secret requirements. See `docs/model-recovery.md` for the required
one-time export of the current model: no production export has been performed by
this implementation. PVC training snapshots are interruption recovery, not S3
disaster-recovery storage.

## Offline checks

```sh
helmfile -e production build > /tmp/bootstrap-state.yaml
helmfile -e production lint
.venv/bin/pytest -q tests/test_bootstrap_gitops.py tests/test_delivery_workflows.py
```

These validate configuration, ownership, target guards, sequencing and idempotence
with local fixtures/mocks. They do not establish live AWS permissions, cluster
capacity, Git access or controller readiness. Live Bootstrap was not executed.
