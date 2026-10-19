# Tear down production infrastructure

When asked to execute this file, tear down this repository's `prod` environment
in `eu-west-2`. This deletes workloads, databases, versioned platform data, and
ECR images. Preserve RDS final snapshots and the entire `terraform/bootstrap/prod`
stack (state bucket, execution role, and GitHub OIDC resources). Do not execute
this file when the request is only to review it or write documentation.

Work from the repository root. Read `AGENTS.md`, `docs/AWS_PRODUCTION_LAUNCH.md`,
and `docs/DATA_GOVERNANCE.md`. If the user requested data retention, resolve that
conflict before deleting data. Never bypass Object Lock or delete unrelated AWS
resources. Do not ask again for an already authorized environment teardown.

## 1. Check identity and preserve recovery information

Use the intended AWS profile and verify `aws sts get-caller-identity` against the
account in the existing production backend and tfvars. Use the existing backend;
do not initialize an empty replacement. Set `AWS_REGION=eu-west-2`.

```sh
umask 077
TEARDOWN_DIR="$(mktemp -d /tmp/mlops-teardown.XXXXXX)"
scripts/terraform.sh -chdir=terraform/environments/prod state pull > "$TEARDOWN_DIR/prod.tfstate"
scripts/terraform.sh -chdir=terraform/environments/prod output -json > "$TEARDOWN_DIR/outputs.json"
scripts/terraform.sh -chdir=terraform/environments/prod state list
CLUSTER_NAME="$(jq -r '.cluster_name.value' "$TEARDOWN_DIR/outputs.json")"
PLATFORM_BUCKET="$(jq -r '.platform_data_bucket.value' "$TEARDOWN_DIR/outputs.json")"
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION"
kubectl config current-context
kubectl get applications -n argocd
kubectl get svc,pvc -A
kubectl get pv
```

Requires AWS CLI, Terraform, `kubectl`, and `jq`. Securely preserve the bootstrap
local state and existing tfvars too. Back up the `airflow-runtime` Secret into
the private recovery directory if it exists; its Fernet key is needed to decrypt
connections in an Airflow database restore. Never display Secret or state contents.
Move recovery files to approved durable private storage before finishing.
If the cluster is already gone, skip Kubernetes cleanup and inspect AWS leftovers.

## 2. Remove Kubernetes resources while controllers still exist

- Stop Airflow work and any running image publication/deployment operations.
- Disable automatic sync on any applications where it was subsequently enabled.
  Remove **only the root application's** Argo resource finalizer, then delete
  `prod-mlops-bootstrap` without cascading to children. This lets you control
  deletion order. Child application finalizers must remain enabled.
  ```sh
  kubectl -n argocd patch application prod-mlops-bootstrap --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":null}},"metadata":{"finalizers":[]}}'
  kubectl -n argocd delete application prod-mlops-bootstrap
  ```
- Delete `airbnb-inference` and `airbnb-platform` Argo Applications and wait for
  their resources to disappear. Then delete the `traefik` Application and wait
  for its LoadBalancer Service and AWS NLB, listeners, target groups, and
  controller-owned security groups to disappear. Keep
  `aws-load-balancer-controller` and `external-dns` running until that completes.
  Check Route 53 for stale records created by ExternalDNS; remove only records
  belonging to this deployment. A nonempty managed zone blocks Terraform deletion.
- Delete monitoring applications and the deployment's remaining PVCs while EBS
  CSI still works. Check PV reclaim policies; delete deployment-owned retained
  EBS volumes only after confirming the intended backup/retention. Confirm volume
  deletion in AWS, since Kubernetes PVC deletion alone is not proof.
- Delete remaining child Applications, keeping their controllers until dependent
  resources are gone. Do not strip stuck Service/PVC/application finalizers to
  make deletion appear successful; resolve their controller/AWS error first.

## 3. Remove Terraform destroy blockers

Both databases have deletion protection enabled and fixed final snapshot names
in `terraform/environments/prod/main.tf`. Use a temporary Terraform override to
disable protection and choose fresh names. Apply this change before destroying
so the final snapshot identifiers are persisted in resource state. If resuming a
partial teardown, reuse its existing override and snapshot names:

```sh
SNAPSHOT_SUFFIX="$(date -u +%Y%m%d%H%M%S)"
cat > terraform/environments/prod/teardown_override.tf <<EOF
module "mlflow_database" {
  deletion_protection       = false
  final_snapshot_identifier = "${CLUSTER_NAME}-mlflow-final-${SNAPSHOT_SUFFIX}"
}
module "airflow_database" {
  deletion_protection       = false
  final_snapshot_identifier = "${CLUSTER_NAME}-airflow-final-${SNAPSHOT_SUFFIX}"
}
EOF
scripts/terraform.sh -chdir=terraform/environments/prod plan \
  -var-file=terraform.local.tfvars \
  -target=module.mlflow_database.aws_db_instance.this \
  -target=module.airflow_database.aws_db_instance.this \
  -out="$TEARDOWN_DIR/prepare-databases.tfplan"
scripts/terraform.sh -chdir=terraform/environments/prod show "$TEARDOWN_DIR/prepare-databases.tfplan"
scripts/terraform.sh -chdir=terraform/environments/prod apply "$TEARDOWN_DIR/prepare-databases.tfplan"
```

Inspect the preparation plan before applying: it must only update protection and
snapshot names on existing databases. Targets can include dependencies; resolve
any unrelated changes before proceeding. Skip missing instances rather than
recreating them. Keep `skip_final_snapshot=false`; never delete previous snapshots
to reuse their names. Verify protection is false and remember the fresh names.
Remove only this temporary override after teardown; do not commit it.

The platform bucket is versioned and Object-Locked, and ECR has
`force_delete=false`. Clean only the bucket/repositories from saved Terraform
outputs; keep the separate bootstrap state bucket intact:

- For each ECR URL in `ecr_repository_urls`, take the repository name after `/`.
  Repeatedly call `aws ecr list-images --repository-name NAME --max-results 100
  --no-paginate`, then `aws ecr batch-delete-image` with those `imageIds` until
  empty. Check the returned `failures`, including failures with exit status zero.
  Let Terraform delete the empty repositories.
- Repeatedly call `aws s3api list-object-versions --bucket "$PLATFORM_BUCKET"
  --max-keys 1000 --no-paginate`. Combine `Versions` and `DeleteMarkers` into
  `{"Objects":[{"Key":"...","VersionId":"..."}]}` batches and pass a private
  JSON file to `aws s3api delete-objects --bucket "$PLATFORM_BUCKET" --delete
  file://PATH`. Check `Errors`, including responses with exit status zero.
  `aws s3 rm --recursive` is insufficient for a versioned bucket.
- If retention/legal hold prevents deletion, preserve those versions and report
  their retention deadline. Never pass `--bypass-governance-retention`. Continue
  the environment destroy to remove other infrastructure, recording that the
  locked bucket/object may fail and remain in Terraform state. Retry their
  deletion after retention expires. Do not loop indefinitely on failed deletes.

## 4. Destroy and verify

```sh
scripts/terraform.sh -chdir=terraform/environments/prod plan -destroy \
  -var-file=terraform.local.tfvars -out="$TEARDOWN_DIR/destroy.tfplan"
scripts/terraform.sh -chdir=terraform/environments/prod show "$TEARDOWN_DIR/destroy.tfplan"
scripts/terraform.sh -chdir=terraform/environments/prod apply "$TEARDOWN_DIR/destroy.tfplan"
scripts/terraform.sh -chdir=terraform/environments/prod state list
```

Inspect the saved plan before applying. Never destroy `terraform/bootstrap/prod`,
remove resources from state to hide failures, or use a blanket account cleanup.
On a failed/partial destroy, inspect the error and remaining state, fix its cause,
then create a new destroy plan. Preserve state throughout.

Verify the deployment's EKS/node instances, RDS instances, Redis, NAT gateways,
EIPs, NLB/target groups, EBS volumes, and VPC are gone. Confirm both final RDS
snapshots exist. Report every remaining resource and why it remains, including
retained Object-Locked data, final snapshots, bootstrap state, scheduled secret
deletions, and any KMS deletion windows. Retained storage/backups can still incur
charges. Do not call a partial destroy complete or claim the AWS bill is zero.
