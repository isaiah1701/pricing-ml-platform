# pricing-ml-platform

## Overview

This project delivers a production-oriented machine learning platform for
predicting prices of New York City Airbnb properties. The prediction service
helps prospective guests form realistic price expectations earlier in their
search, reducing decision friction and making it easier to identify properties
that fit their needs and budget. For hosts and operators, more informed users
can translate into stronger engagement and improved booking conversion.

The platform is designed to take the model beyond experimentation and support
reliable operation at scale. It includes experiment tracking and feature
management, controlled production deployments, rollback mechanisms, and
observability across the model and its serving infrastructure. These
capabilities provide the operational foundation needed to release changes
safely, identify degradation quickly, and maintain confidence in the
predictions presented to users.

The engineering decisions documented in this repository are evaluated against
the outcomes that matter to the business: cost efficiency, service
reliability, scalability, and the ability for wider engineering and data
teams to adopt and operate the platform consistently. The documentation that
follows explains the architecture, delivery model, monitoring approach, and
governance decisions behind that operating model.

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
