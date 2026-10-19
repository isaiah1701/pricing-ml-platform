# prod-mlops-platform

NYC Airbnb price training, MLflow experiment tracking/registry, and a
production-shaped local-to-EKS serving path.

- [Model registry and local serving](docs/SERVING.md)
- [Docker, Helm, Argo CD, secrets, and Terraform definitions](docs/DEPLOYMENT.md)
- [Infrastructure](docs/INFRASTRUCTURE.md)
- [AWS production launch](docs/AWS_PRODUCTION_LAUNCH.md)
- [Monitoring, dashboards, and alerts](docs/MONITORING.md)
- [Retraining, approval, promotion, and rollback](docs/RETRAINING.md)
- [Canary delivery, CI/CD, and GitOps](docs/CANARY_DELIVERY.md)
- [Data validation, immutable snapshots, and lineage](docs/DATA_GOVERNANCE.md)
- [Model problem and experiment history](docs/PROBLEM.md)
- AI runbooks: [Launch infrastructure](skills/launch.md) · [Tear down infrastructure](skills/teardown.md)
- [k6 load test: 100 users](load-tests/README.md)

## Local disk hygiene

Terraform providers and repeated container builds are the largest local disk
consumers. Run Terraform through the cache-aware wrapper so provider binaries
are shared between root modules:

```sh
cd terraform/environments/prod
../../../scripts/terraform.sh init -backend=false
```

Remove reproducible repository artifacts after infrastructure validation:

```sh
scripts/cleanup_local_artifacts.sh
```

Use the Compose wrapper for local builds and stack updates. After a successful
update it automatically prunes superseded images belonging to this project:

```sh
scripts/compose.sh up --build --detach
```

For manual builds or periodic deep cleanup, remove unused images for this
Compose project and clear uv's reinstallable package cache:

```sh
scripts/cleanup_local_artifacts.sh --docker --global-cache
```

The cleanup intentionally preserves datasets, MLflow runs and models, `.venv`,
Docker volumes, and every image referenced by a container. If Docker uses a
non-default context, prefix the command with `DOCKER_CONTEXT=<context>`.
