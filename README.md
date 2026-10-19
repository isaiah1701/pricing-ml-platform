# Pricing ML Platform

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

## Platform demo

See the platform flow from training evidence to a controlled production
prediction.

https://github.com/user-attachments/assets/300feb1d-6ac5-449f-8c45-eb080d920a61

For the end-to-end explanation of how a model moves from training to
production, including the promotion, rollback, and retraining loop, see the
[model lifecycle section](docs/ARCHITECTURE.md#model-lifecycle-training-to-production).
The [prediction API documentation](docs/ARCHITECTURE.md#prediction-api)
describes the serving endpoints, request contract, responses, and error
statuses.

## Evidence and demos

| Deployment control | Experiment evidence | Monitoring evidence |
|---|---|---|
| ![Argo CD shows healthy and synced platform applications](media/MLArgoCD.png) | ![MLflow CatBoost run with metrics and registered model version](media/MLRunScreenshot.png) | ![Airflow drift-monitoring runs completing successfully](media/airflowRuns.png) |
| Healthy, synced applications make releases safer and recovery faster. | Recorded metrics, lineage, and versioning make model decisions auditable. | Successful scheduled checks surface issues before they affect users. |

### Video demos

| Demo | Focus | Business outcome |
|---|---|---|
| Platform walkthrough | Training, approval, release, and serving | Shows how a governed model becomes a production prediction. |
| Grafana dashboards | Service health, latency, capacity, drift, and quality | Gives operators one view for fast, evidence-based action. |

#### Grafana dashboards

See service health, latency, capacity, drift, and model quality in one view.

https://github.com/user-attachments/assets/5f17e3db-6a54-43fc-8e81-f1077c602f9e

## 1. Prerequisites — tools and accounts

For local development, install Git, Docker with Compose, Python 3.10+, and
[uv](https://docs.astral.sh/uv/). For AWS deployment, you also need an AWS
account with permission to create the required infrastructure, the AWS CLI,
Terraform, `kubectl`, and Helm. GitHub Actions deployment requires repository
Actions permissions plus the configured AWS OIDC, ECR, and production
environment variables.

## 2. Quick start — deploy from a fresh clone

```sh
git clone https://github.com/isaiah1701/pricing-ml-platform.git
cd pricing-ml-platform
uv sync --extra test --extra orchestration
scripts/compose.sh up --build --detach
```

This starts the local platform. Validate it with `uv run pytest`. Production
deployment is performed through the Terraform and GitHub Actions workflows
after AWS credentials, repository variables, and the production environment
have been configured.
