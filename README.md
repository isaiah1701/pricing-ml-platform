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
