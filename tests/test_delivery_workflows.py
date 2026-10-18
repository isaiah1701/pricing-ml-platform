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
        (PROJECT_ROOT / ".github/workflows/docker.yml").read_text(),
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


def test_validation_workflows_check_all_required_surfaces() -> None:
    """Retain Python, Terraform and Kubernetes validation in their own workflows."""
    ci = (PROJECT_ROOT / ".github/workflows/docker.yml").read_text()
    terraform = (PROJECT_ROOT / ".github/workflows/terraform-plan.yml").read_text()
    kubernetes = (PROJECT_ROOT / ".github/workflows/kubernetes.yml").read_text()
    assert "ruff check ." in ci and "uv run pytest" in ci
    assert "terraform/environments/prod" in terraform
    assert "terraform/bootstrap/prod" in terraform and "-backend=false" in terraform
    assert "helm lint" in kubernetes and "kubeconform" in kubernetes
    assert "terraform" not in workflow("docker")["jobs"]


def test_publish_workflow_is_oidc_immutable_and_gitops_only() -> None:
    """Push SHA images after scanning and update Git without cluster mutation."""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "docker.yml").read_text(
        encoding="utf-8"
    )
    configuration = yaml.load(workflow, Loader=yaml.BaseLoader)
    assert set(configuration["on"]) == {"workflow_dispatch", "push", "pull_request"}
    assert "training/preprocessing/**" not in configuration["on"]["push"]["paths"]
    condition = configuration["jobs"]["publish-production"]["if"]
    assert "vars.AWS_PUBLISH_ENABLED == 'true'" in condition
    assert "github.event_name != 'pull_request'" in condition
    assert "refs/heads/main" in condition
    assert "id-token: write" in workflow
    assert "environment: production" in workflow
    assert "sha-${GITHUB_SHA}" in workflow
    assert workflow.count("build-args: GIT_COMMIT=${{ github.sha }}") == 2
    assert workflow.count("aquasecurity/trivy-action") == 1
    assert "steps.published.outputs.digest" in workflow
    assert "needs: [validate, changes, build]" in workflow
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
        ("serving/app/api.py", {"docker"}),
        ("training/preprocessing/clean.py", {"ml-data", "docker"}),
        ("feature_repo/features.py", {"ml-data", "docker"}),
        ("pipelines/training.py", {"ml-training", "docker"}),
        ("training/train.py", {"ml-training", "docker"}),
    ],
)
def test_pipeline_push_filters(path: str, expected: set[str]) -> None:
    """Exercise representative changes against positive and negative path filters."""
    triggered: set[str] = set()
    for name in ("ml-data", "ml-training", "docker"):
        matches = False
        for pattern in workflow(name)["on"]["push"]["paths"]:
            negative = pattern.startswith("!")
            if fnmatchcase(path, pattern.lstrip("!")):
                matches = not negative
        if matches:
            triggered.add(name)
    assert triggered == expected


@pytest.mark.parametrize("name", ["ml-data", "ml-training"])
def test_expensive_execution_requires_manual_opt_in(name: str) -> None:
    """Prevent code pushes and ordinary pull requests from executing production DAGs."""
    config = workflow(name)
    assert config["on"]["workflow_dispatch"]["inputs"]["execute"]["default"] == "false"
    condition = config["jobs"]["execute"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "inputs.execute" in condition
    assert "refs/heads/main" in condition
    runner = config["jobs"]["execute"]
    assert runner["environment"] == "production"
    assert "ML_PIPELINE_EXECUTION_ENABLED" in runner["if"]


def test_data_changes_do_not_build_serving_runtime() -> None:
    """Keep preprocessing-only changes out of serving Docker validation."""
    config = workflow("docker")
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
        for step in config["jobs"]["build"]["steps"]
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


def test_workflow_inventory_has_three_ml_pipelines_and_no_k6_pipeline() -> None:
    """Expose exactly three ML entry points plus clearly separated platform workflows."""
    folder = PROJECT_ROOT / ".github/workflows"
    assert {path.stem for path in folder.glob("ml-*.yml")} == {
        "ml-data",
        "ml-training",
        "ml-deploy",
    }
    assert not (folder / "k6.yml").exists()
    for name in ("docker", "kubernetes", "terraform-plan", "terraform-apply"):
        assert (folder / f"{name}.yml").is_file()
    assert (
        workflow("ml-deploy")["jobs"]["deploy"]["uses"]
        == "./.github/workflows/kubernetes.yml"
    )
    assert "build-push-action" not in (folder / "ml-deploy.yml").read_text()


def test_apply_is_manual_and_consumes_only_a_trusted_saved_plan() -> None:
    """Keep approval, run provenance and artifact selection on the apply path."""
    config = workflow("terraform-apply")
    assert set(config["on"]) == {"workflow_dispatch"}
    job = config["jobs"]["apply"]
    assert "inputs.confirm_apply" in job["if"]
    assert "TF_APPLY_ENABLED" in job["if"]
    assert job["environment"] == "production"
    script = next(
        step["with"]["script"]
        for step in job["steps"]
        if step.get("id") == "provenance"
    )
    for field in (
        "run.path",
        "run.head_sha",
        "run.head_branch",
        "run.conclusion",
        "run.event",
        "run.head_repository",
    ):
        assert field in script
    download = next(
        step for step in job["steps"] if "download-artifact" in step.get("uses", "")
    )
    assert download["with"]["run-id"] == "${{ inputs.plan_run_id }}"
    assert (
        job["concurrency"]["group"]
        == workflow("terraform-plan")["jobs"]["plan"]["concurrency"]["group"]
    )


def test_kubernetes_deploy_requires_main_manual_opt_in() -> None:
    """Prevent PR validation and ordinary pushes from syncing live applications."""
    config = workflow("kubernetes")
    assert config["on"]["workflow_dispatch"]["inputs"]["deploy"]["default"] == "false"
    deploy = config["jobs"]["deploy"]
    assert "inputs.deploy" in deploy["if"] and "K8S_DEPLOY_ENABLED" in deploy["if"]
    assert "refs/heads/main" in deploy["if"]
    assert deploy["environment"] == "production"
