"""Policy tests for CI, immutable publishing, and GitOps reconciliation."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_validation_workflow_checks_all_required_surfaces() -> None:
    """Require full Python, Terraform, Helm, and rendered-manifest validation."""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )
    assert "ruff check ." in workflow
    assert "uv run pytest" in workflow
    assert "terraform/environments/prod" in workflow
    assert "terraform/bootstrap/prod" in workflow
    assert "for environment in dev staging prod" not in workflow
    assert "-backend=false" in workflow
    assert "terraform" in workflow and " apply" not in workflow
    assert "helm lint" in workflow
    assert "kubeconform" in workflow


def test_publish_workflow_is_oidc_immutable_and_gitops_only() -> None:
    """Push SHA images after scanning and update Git without cluster mutation."""
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "publish-images.yml"
    ).read_text(encoding="utf-8")
    configuration = yaml.load(workflow, Loader=yaml.BaseLoader)
    assert set(configuration["on"]) == {"workflow_dispatch"}
    assert configuration["jobs"]["publish-production"]["if"] == (
        "${{ vars.AWS_PUBLISH_ENABLED == 'true' }}"
    )
    assert "id-token: write" in workflow
    assert "environment: production" in workflow
    assert "sha-${GITHUB_SHA}" in workflow
    assert workflow.count("build-args: GIT_COMMIT=${{ github.sha }}") == 2
    assert workflow.count("aquasecurity/trivy-action") == 2
    assert "docker push" in workflow
    assert "create-pull-request" in workflow
    assert "serving/helm/values/production.yaml" in workflow
    assert "kubectl apply" not in workflow.lower()
    assert "kubectl set image" not in workflow.lower()


def test_gitops_requires_manual_sync_before_launch() -> None:
    """Prevent repository pushes from enabling automatic cluster reconciliation."""
    values = yaml.safe_load(
        (PROJECT_ROOT / "serving" / "helm" / "values.yaml").read_text(
            encoding="utf-8"
        )
    )
    root_application = yaml.safe_load(
        (PROJECT_ROOT / "serving" / "argocd" / "root-application.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert values["syncPolicy"]["automated"] is False
    assert root_application["spec"]["syncPolicy"]["automated"]["enabled"] is False
