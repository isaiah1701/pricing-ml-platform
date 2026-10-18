"""Verify release boundaries and controller/digest checks with offline fixtures."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from scripts import sync_gitops, terraform_ci, verify_inference, verify_rollout

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def test_controllers_follow_existing_gitops_waves() -> None:
    """Render actual controller Applications without installing any releases."""
    applications = sync_gitops.controller_applications()
    names = {item["metadata"]["name"] for item in applications}
    assert {
        "cert-manager",
        "traefik",
        "external-secrets",
        "argo-rollouts",
        "aws-load-balancer-controller",
    } <= names
    assert not names & {"airbnb-platform", "airbnb-inference", "argocd"}
    waves = [
        int(item["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        for item in applications
    ]
    assert waves == sorted(waves)
    assert all(item["spec"]["source"].get("chart") for item in applications)


def test_sync_does_not_accept_old_success_at_the_same_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait for this operation's marker even when chart versions have not changed."""
    source = {
        "repoURL": "https://charts.example",
        "chart": "controller",
        "targetRevision": "1.2.3",
    }
    old = {
        "spec": {"source": source},
        "status": {
            "operationState": {
                "phase": "Succeeded",
                "syncResult": {"revision": "1.2.3"},
                "operation": {"info": [{"name": "mlops-request-id", "value": "old"}]},
            },
            "sync": {"status": "Synced", "revision": "1.2.3"},
            "health": {"status": "Healthy"},
        },
    }
    current = json.loads(json.dumps(old))
    current["status"]["operationState"]["operation"]["info"][0]["value"] = "new"
    read = Mock(side_effect=[old, old, current])
    monkeypatch.setattr(sync_gitops, "_application", read)
    monkeypatch.setattr(
        sync_gitops.uuid, "uuid4", Mock(return_value=SimpleNamespace(hex="new"))
    )
    monkeypatch.setattr(sync_gitops.subprocess, "run", Mock())
    monkeypatch.setattr(sync_gitops.time, "sleep", Mock())
    sync_gitops.sync(
        "controller", "1.2.3", "isaiah1701/prod-mlops-platform", declared_source=source
    )
    assert read.call_count == 3


@pytest.mark.parametrize(
    "case", ["correct", "legacy-tag", "wrong-reference", "wrong-digest", "not-ready"]
)
def test_release_checks_actual_pod_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Verify GitOps references and ready pod bytes, retaining immutable legacy tag support."""
    repository = "123456789012.dkr.ecr.eu-west-2.amazonaws.com/serving"
    digest = "sha256:" + "a" * 64
    image = {"repository": repository, "tag": "sha-" + SHA}
    if case != "legacy-tag":
        image["digest"] = digest
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"image": image}))
    monkeypatch.setattr(verify_rollout, "VALUES", values)
    reference = (
        f"{repository}:{image['tag']}"
        if case == "legacy-tag"
        else f"{repository}@{digest}"
    )
    rollout = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "inference",
                            "image": reference
                            if case != "wrong-reference"
                            else "wrong-image",
                        }
                    ]
                }
            }
        },
        "status": {"currentPodHash": "desired"},
    }
    pods = {
        "items": [
            {
                "status": {
                    "containerStatuses": [
                        {
                            "name": "inference",
                            "ready": case != "not-ready",
                            "imageID": f"{repository}@{digest if case != 'wrong-digest' else 'sha256:wrong'}",
                        }
                    ]
                }
            }
        ]
    }
    responses = [json.dumps(rollout), json.dumps(pods)]
    if case == "legacy-tag":
        responses.insert(0, digest)
    monkeypatch.setattr(
        verify_rollout.subprocess, "check_output", Mock(side_effect=responses)
    )
    if case in {"correct", "legacy-tag"}:
        verify_rollout.verify_image()
    else:
        with pytest.raises(ValueError):
            verify_rollout.verify_image()


def test_model_refresh_is_allowed_before_failing_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait for the existing alias-refresh loop instead of rejecting a temporarily old model."""
    check = Mock(side_effect=[ValueError("old version"), None])
    monkeypatch.setattr(verify_inference, "verify", check)
    monkeypatch.setattr(verify_inference.time, "monotonic", Mock(side_effect=[0, 0, 1]))
    monkeypatch.setattr(verify_inference.time, "sleep", Mock())
    verify_inference.wait_for_inference("https://serving.test", "2", timeout=3)
    assert check.call_count == 2


def test_terraform_rejects_wrong_aws_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a role configured for another account before any plan/apply command."""
    monkeypatch.setenv("EXPECTED_AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setattr(
        terraform_ci.subprocess,
        "check_output",
        Mock(return_value='{"Account":"999999999999"}'),
    )
    with pytest.raises(ValueError, match="account"):
        terraform_ci.expected_account()


@pytest.mark.parametrize(
    "case", ["valid", "fork", "pr", "failed", "stale", "wrong-workflow"]
)
def test_apply_workflow_run_provenance_is_enforced(case: str) -> None:
    """Execute the actual GitHub API guard with trusted and untrusted run fixtures."""
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "Node is unavailable; GitHub runners provide the github-script runtime"
        )
    config = yaml.load(
        (ROOT / ".github/workflows/terraform-apply.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    script = next(
        step["with"]["script"]
        for step in config["jobs"]["apply"]["steps"]
        if step.get("id") == "provenance"
    )
    run = {
        "path": ".github/workflows/terraform-plan.yml",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": SHA,
        "event": "push",
        "head_repository": {"full_name": "isaiah1701/prod-mlops-platform"},
    }
    if case == "fork":
        run["head_repository"] = {"full_name": "someone/fork"}
    elif case == "pr":
        run["event"] = "pull_request"
    elif case == "failed":
        run["conclusion"] = "failure"
    elif case == "wrong-workflow":
        run["path"] = ".github/workflows/other.yml"
    context_sha = "b" * 40 if case == "stale" else SHA
    harness = (
        "const fixture="
        + json.dumps(run)
        + "; const github={rest:{actions:{getWorkflowRun:async()=>({data:fixture})}}};"
        + "const context={sha:"
        + json.dumps(context_sha)
        + ",repo:{owner:'isaiah1701',repo:'prod-mlops-platform'}};"
        + "const core={setOutput:()=>{}}; (async()=>{"
        + script
        + "})().catch(()=>process.exit(1));"
    )
    result = subprocess.run(
        [node, "-e", harness],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PLAN_RUN_ID": "123", "PLAN_SOURCE_SHA": SHA},
    )
    assert (result.returncode == 0) is (case == "valid")
