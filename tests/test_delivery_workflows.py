"""Policy tests for CI, immutable publishing, and GitOps reconciliation."""

from __future__ import annotations

import logging
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
import yaml

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_image_promotion_passes_expression_and_filename_to_yq() -> None:
    """Check the folded YAML command preserves the expression and file arguments."""
    LOGGER.info("Validating the image-promotion shell argument boundaries")
    configuration = yaml.load(
        (PROJECT_ROOT / ".github/workflows/publish-images.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    step = next(
        step
        for step in configuration["jobs"]["publish-production"]["steps"]
        if step.get("name") == "Update environment values only"
    )
    command = step["with"]["cmd"]
    result = subprocess.run(
        [
            "bash",
            "-ec",
            'yq() { test "$#" -eq 3 && test "$3" = "serving/helm/values/production.yaml"; }; '
            + command,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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
    assert set(configuration["on"]) == {"workflow_dispatch", "push"}
    assert "training/preprocessing/**" not in configuration["on"]["push"]["paths"]
    assert configuration["jobs"]["publish-production"]["if"] == (
        "${{ vars.AWS_PUBLISH_ENABLED == 'true' }}"
    )
    assert "id-token: write" in workflow
    assert "environment: production" in workflow
    assert "sha-${GITHUB_SHA}" in workflow
    assert workflow.count("build-args: GIT_COMMIT=${{ github.sha }}") == 1
    assert workflow.count("aquasecurity/trivy-action") == 1
    assert "steps.published.outputs.digest" in workflow
    assert "needs: validate" in workflow
    assert "docker push" in workflow
    assert "create-pull-request" in workflow
    assert "serving/helm/values/production.yaml" in workflow
    assert "kubectl apply" not in workflow.lower()
    assert "kubectl set image" not in workflow.lower()


def test_gitops_requires_manual_sync_before_launch() -> None:
    """Prevent repository pushes from enabling automatic cluster reconciliation."""
    values = yaml.safe_load(
        (PROJECT_ROOT / "serving" / "helm" / "values.yaml").read_text(encoding="utf-8")
    )
    root_application = yaml.safe_load(
        (PROJECT_ROOT / "serving" / "argocd" / "root-application.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert values["syncPolicy"]["automated"] is False
    assert root_application["spec"]["syncPolicy"]["automated"]["enabled"] is False


def workflow(name: str) -> dict[str, Any]:
    """Read a workflow while preserving the YAML on key as text."""
    return yaml.load(
        (PROJECT_ROOT / f".github/workflows/{name}.yml").read_text(),
        Loader=yaml.BaseLoader,
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/architecture.md", set()),
        ("terraform/modules/eks/main.tf", set()),
        ("serving/app/api.py", {"publish-images"}),
        ("training/preprocessing/clean.py", {"data"}),
        ("feature_repo/features.py", {"data"}),
        ("pipelines/training.py", {"training"}),
        ("training/train.py", {"training", "publish-images"}),
    ],
)
def test_pipeline_push_filters(path: str, expected: set[str]) -> None:
    """Exercise representative changes against positive and negative path filters."""
    triggered: set[str] = set()
    for name in ("data", "training", "publish-images"):
        matches = False
        for pattern in workflow(name)["on"]["push"]["paths"]:
            negative = pattern.startswith("!")
            if fnmatchcase(path, pattern.lstrip("!")):
                matches = not negative
        if matches:
            triggered.add(name)
    assert triggered == expected


@pytest.mark.parametrize("name", ["data", "training"])
def test_expensive_execution_requires_manual_opt_in(name: str) -> None:
    """Prevent code pushes and ordinary pull requests from executing production DAGs."""
    config = workflow(name)
    assert config["on"]["workflow_dispatch"]["inputs"]["execute"]["default"] == "false"
    condition = config["jobs"]["execute"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "inputs.execute" in condition
    assert "refs/heads/main" in condition
    runner = workflow("airflow-run")["jobs"]["run"]
    assert runner["environment"] == "production"
    assert "ML_PIPELINE_EXECUTION_ENABLED" in runner["if"]


def test_data_changes_do_not_build_serving_runtime() -> None:
    """Keep preprocessing-only changes out of serving Docker validation."""
    config = workflow("validate")
    steps = config["jobs"]["changes"]["steps"]
    filters = yaml.safe_load(
        next(step["with"]["filters"] for step in steps if "id" in step)
    )
    assert not any(
        fnmatchcase("training/preprocessing/clean.py", pattern)
        for pattern in filters["serving"]
    )
    image = next(
        step
        for step in config["jobs"]["image"]["steps"]
        if step.get("name") == "Build local candidate image"
    )
    assert "outputs.serving" in image["if"]


def test_helm_selects_digests_and_rejects_invalid_digest() -> None:
    """Render both runtimes using their digest while retaining legacy tag support."""
    digest = "sha256:" + "a" * 64
    command = [
        "helm",
        "template",
        "test",
        str(PROJECT_ROOT / "serving/helm"),
        "-f",
        str(PROJECT_ROOT / "serving/helm/values/production.yaml"),
        "--set",
        "image.repository=airbnb-price-predictor",
        "--set",
        "trainingImage.repository=airbnb-model-training",
        "--set",
        f"image.digest={digest}",
        "--set",
        f"trainingImage.digest={digest}",
        "--set",
        "platform.enabled=true",
    ]
    rendered = subprocess.run(command, check=True, capture_output=True, text=True)
    assert f"airbnb-price-predictor@{digest}" in rendered.stdout
    assert f"airbnb-model-training@{digest}" in rendered.stdout
    rejected = subprocess.run(
        command + ["--set", "image.digest=not-a-digest"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "image.digest must be a sha256 digest" in rejected.stderr
