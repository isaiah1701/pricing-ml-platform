# Autoscaling proposal — not deployable yet

This directory is intentionally outside the GitOps application paths. It renders
a NodePool/EC2NodeClass design and an opt-in HPA overlay; it creates no AWS or
Kubernetes resources. Production still has one fixed worker and no HPA.

## Existing platform and intended behavior

`cost_optimized=true` already sets the r5a.large managed node group to
min=desired=max=1. Metrics Server is configured in the bootstrap chart and Cluster
Autoscaler is disabled. Terraform still contains unused Cluster Autoscaler IAM
and ASG discovery tags; do not enable that controller alongside Karpenter.

The proposal keeps the fixed worker for Argo, DNS, storage controllers, MLflow,
Airflow and Karpenter itself. The HPA targets only the inference Rollout, from
one to two replicas, with a 60-second scale-up and 300-second scale-down window.
Its CPU target is provisional and depends on the inference container's CPU
request, not node or training CPU. Tune requests and thresholds from actual
local/test measurements before activation.

Karpenter would act only when a replica cannot be scheduled using pod resource
requests. It does not add a node merely because training consumes its CPU limit.
The extra node has a serving-only taint, which inference already tolerates;
Airflow does not tolerate it. Both nodes use `workload=system` to match the
existing inference selector. Keep training in the existing bounded Airflow
LocalExecutor with max_active_runs=1 and the existing CPU/memory limits. Do not
raise those limits or add required anti-affinity that forces every replica onto
a different node. If both inference replicas fit on the fixed node, no extra
node should be requested.

## Controller, IAM and bootstrap work still required

Install one pinned, EKS-compatible Karpenter controller/CRD release after review.
Place its pod on the managed worker using `nodeSelector: {workload: system}` plus
required node affinity `karpenter.sh/nodepool DoesNotExist`; otherwise it could
move onto the disposable burst node. Give it bounded requests within the fixed
worker's measured capacity. Do not add Fargate or another always-on node.

This is not highly available. If the fixed node fails, Karpenter cannot run to
recover it: the managed node group must replace that node first. Preserve its
min/desired=1 and its EKS/add-on bootstrap dependencies. Do not rely on Karpenter
to provision its own initial worker. The
[official setup guide](https://karpenter.sh/docs/getting-started/getting-started-with-karpenter/)
places the controller on managed nodes or Fargate.

Provision a dedicated controller identity through the existing Terraform EKS Pod
Identity pattern (or reviewed IRSA), scoped to this cluster's node lifecycle,
discovery and tags. Restrict `iam:PassRole` to its worker role; do not reuse the
ECR GitHub OIDC role. Review the pinned release's upstream controller policy
rather than inventing a reduced policy that fails at runtime. Create a worker
role/instance profile with the EKS worker and ECR pull permissions, configure
EKS node access, and verify the existing VPC CNI identity. The candidate NodeClass
uses an explicit profile, AMI ID, existing private subnets and node security group;
it does not infer these from unrelated tags. See the
[NodeClass identity requirements](https://karpenter.sh/docs/concepts/nodeclasses/#specinstanceprofile).

Install CRDs/controller first, wait for readiness, then consider NodeClass and
NodePool. Neither controller IAM, node access nor installation is implemented
here; no running NodePool is claimed. On-demand capacity avoids needing a new
Spot interruption queue for this proposal, but planned/forced AWS interruptions
and security patching still need an operator procedure.

## Budget and disruption blocker

The candidate selects exactly r5a.large and limits the dynamic pool to 2 vCPU /
16 GiB: nominally one extra node, plus one fixed worker. This is **not a hard
two-node guarantee**. Karpenter's
[NodePool documentation](https://karpenter.sh/docs/concepts/nodepools/#speclimits)
states that parallel provisioning can temporarily exceed limits. Replacement,
drift and managed-node-group updates also need a reviewed no-surge procedure.
Do not enable this proposal until the strict global ceiling has an enforceable,
tested design. Setting `limits.nodes` or a CPU limit is not proof of that design.

Consolidation is proposed after five minutes of low demand, with one-at-a-time
disruption. Expiration is disabled; plan explicit patch rotation. PDBs, pending
pods and controller availability can delay consolidation, so do not promise a
fixed scale-in time. Check the inference PDB/canary behavior and confirm the
remaining replica fits on the managed worker before eviction. The extra node's
taint keeps Airflow and its EBS volumes from blocking consolidation.

## Offline checks and remaining activation gates

```sh
terraform -chdir=docs/proposals/autoscaling init -backend=false
terraform -chdir=docs/proposals/autoscaling validate
terraform -chdir=docs/proposals/autoscaling test
.venv/bin/pytest -q tests/test_autoscaling_proposal.py tests/test_canary_delivery.py
helm template airbnb-inference serving/helm \
  -f serving/helm/values/production.yaml \
  -f docs/proposals/autoscaling/serving-values.yaml > /tmp/hpa-proposal.yaml
```

Terraform tests use fake coordinates and validate the proposal's output
contract; they do not validate EC2 availability, IAM or Karpenter CRD acceptance.
Helm tests render the real Rollout/HPA, replica bounds and scaling behavior, while
the existing budget test proves production still has fixed capacity. The chart
omits `.spec.replicas` when HPA is enabled so it does not reset HPA's desired size.
Before activation, configure the inference Argo application's ignoreDifferences
for Rollout `/spec/replicas` with RespectIgnoreDifferences and verify ownership
using the pinned Argo version.

Verify Metrics Server's APIService and `kubectl top pods` on the intended cluster;
configured does not mean functional. Validate the pinned Karpenter CRD schemas,
NodeClass readiness, controller IAM, instance AMI/bootstrap, capacity/daemonset
requests and Pod Identity networking. Test idle baseline, training-only CPU,
inference overload, full-node pending pods, return to idle, controller loss and
node replacement in an approved test environment. A third EC2 instance, including
replacement instances, fails the budget requirement.

Implementation and deployment need a separate approved infrastructure change.
This proposal is the Phase 5 fallback permitted by the refactor brief, not a
completed autoscaler rollout.
