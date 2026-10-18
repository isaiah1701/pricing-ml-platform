"""Offline verification of Terraform handoff, GitOps sync and inference release checks."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts import sync_gitops, terraform_ci, verify_inference

LOGGER = logging.getLogger(__name__)
SHA = "a" * 40


@pytest.fixture
def plan_bundle(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Build a fake plan plus valid metadata without AWS or a real Terraform state."""
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "tfplan").write_bytes(b"opaque saved plan")
    lock = tmp_path / ".terraform.lock.hcl"
    lock.write_text("provider lock fixture")
    backend = {
        "bucket": "existing-state",
        "key": "prod/state",
        "region": "eu-west-2",
        "encrypt": True,
        "use_lockfile": True,
    }
    metadata = {
        "schema_version": 1,
        "environment": "production",
        "aws_account": "123456789012",
        "repository": "isaiah1701/prod-mlops-platform",
        "source_sha": SHA,
        "run_id": "123",
        "backend_sha256": terraform_ci.backend_identity(backend),
        "terraform_version": "1.11.0",
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "plan_sha256": hashlib.sha256((directory / "tfplan").read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (directory / "metadata.json").write_text(json.dumps(metadata))
    return directory, lock, backend


def check_bundle(bundle: tuple[Path, Path, dict[str, Any]]) -> dict[str, Any]:
    """Verify a fixture with the exact approved commit, run and production backend."""
    directory, lock, backend = bundle
    return terraform_ci.verify_plan(
        directory,
        repository="isaiah1701/prod-mlops-platform",
        source_sha=SHA,
        run_id="123",
        backend=backend,
        terraform_version="1.11.0",
        lockfile=lock,
        account="123456789012",
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "plan",
        "commit",
        "repository",
        "run",
        "backend",
        "lock",
        "version",
        "expired",
        "account",
        "environment",
    ],
)
def test_apply_refuses_unreviewed_or_corrupt_plan(
    plan_bundle: tuple[Path, Path, dict[str, Any]],
    corruption: str,
) -> None:
    """Reject each meaningful provenance mismatch before any infrastructure command."""
    directory, lock, _ = plan_bundle
    assert check_bundle(plan_bundle)["source_sha"] == SHA
    metadata = json.loads((directory / "metadata.json").read_text())
    if corruption == "plan":
        (directory / "tfplan").write_bytes(b"substituted plan")
    elif corruption == "lock":
        lock.write_text("different provider lock")
    elif corruption == "expired":
        metadata["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
    else:
        field = {
            "commit": "source_sha",
            "repository": "repository",
            "run": "run_id",
            "backend": "backend_sha256",
            "account": "aws_account",
            "environment": "environment",
            "version": "terraform_version",
        }[corruption]
        metadata[field] = "different"
    (directory / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        check_bundle(plan_bundle)


def test_apply_uses_only_saved_binary_without_a_new_plan(
    plan_bundle: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Execute the apply control flow against mocked Terraform and inspect its exact commands."""
    directory, lock, backend = plan_bundle
    for key, value in {
        "TF_STATE_BUCKET": backend["bucket"],
        "TF_STATE_KEY": backend["key"],
        "TF_STATE_REGION": backend["region"],
        "GITHUB_REPOSITORY": "isaiah1701/prod-mlops-platform",
        "PLAN_SOURCE_SHA": SHA,
        "PLAN_RUN_ID": "123",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        terraform_ci, "expected_account", Mock(return_value="123456789012")
    )
    monkeypatch.setattr(terraform_ci, "ROOT", lock.parent)
    runner = Mock(side_effect=[json.dumps({"terraform_version": "1.11.0"}), "", ""])
    monkeypatch.setattr(terraform_ci, "_run", runner)
    terraform_ci.execute("apply", directory)
    commands = [call.args[0] for call in runner.call_args_list]
    assert [command[0] for command in commands] == ["version", "init", "apply"]
    assert commands[-1] == ["apply", "-input=false", str(directory / "tfplan")]
    assert (
        tmp_path / "mlops-terraform-runtime/backend.json"
    ).stat().st_mode & 0o777 == 0o600


def test_plan_captures_provenance_without_exporting_sensitive_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Publish only the binary/identity/change addresses and keep tfvars off artifacts and logs."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".terraform.lock.hcl").write_text("locked providers")
    directory = tmp_path / "artifact"
    environment = {
        "TF_STATE_BUCKET": "existing",
        "TF_STATE_KEY": "prod/state",
        "TF_STATE_REGION": "eu-west-2",
        "TF_PROD_TFVARS_JSON": '{"cost_optimized":true,"secret_test":"never-log-this"}',
        "GITHUB_REPOSITORY": "isaiah1701/prod-mlops-platform",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "123",
        "RUNNER_TEMP": str(tmp_path),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        terraform_ci, "expected_account", Mock(return_value="123456789012")
    )
    monkeypatch.setattr(terraform_ci, "ROOT", project)

    def fake_run(arguments: list[str], runtime: Path) -> str:
        """Simulate Terraform while placing a saved-plan fixture on disk."""
        del runtime
        if arguments[0] == "version":
            return '{"terraform_version":"1.11.0"}'
        if arguments[0] == "plan":
            (directory / "tfplan").write_bytes(b"opaque-plan")
        if arguments[0] == "show":
            return json.dumps(
                {
                    "resource_changes": [
                        {
                            "address": "aws_instance.example",
                            "change": {
                                "actions": ["update"],
                                "after": {"secret": "never-log-this"},
                            },
                        }
                    ]
                }
            )
        return ""

    monkeypatch.setattr(terraform_ci, "_run", fake_run)
    terraform_ci.execute("plan", directory)
    assert {path.name for path in directory.iterdir()} == {
        "tfplan",
        "metadata.json",
        "changes.json",
    }
    assert "never-log-this" not in caplog.text
    assert "never-log-this" not in (directory / "changes.json").read_text()
    assert "never-log-this" not in (directory / "metadata.json").read_text()


def application_status(revision: str) -> dict[str, Any]:
    """Return a healthy fixture application with a known Git source."""
    return {
        "spec": {
            "source": {
                "repoURL": "https://github.com/isaiah1701/prod-mlops-platform.git",
                "path": "serving/helm",
                "targetRevision": "main",
            }
        },
        "status": {
            "operationState": {
                "operation": {
                    "info": [{"name": "mlops-request-id", "value": "test-request"}]
                },
                "phase": "Succeeded",
                "syncResult": {"revision": revision},
            },
            "sync": {"status": "Synced", "revision": revision},
            "health": {"status": "Healthy"},
        },
    }


def test_gitops_waits_for_exact_reviewed_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not mistake old healthy status for completion of the new deployment."""
    monkeypatch.setattr(
        sync_gitops.uuid,
        "uuid4",
        Mock(return_value=type("Request", (), {"hex": "test-request"})()),
    )
    read = Mock(
        side_effect=[
            application_status("b" * 40),
            application_status("b" * 40),
            application_status(SHA),
        ]
    )
    patch = Mock()
    monkeypatch.setattr(sync_gitops, "_application", read)
    monkeypatch.setattr(sync_gitops.subprocess, "run", patch)
    monkeypatch.setattr(sync_gitops.time, "sleep", Mock())
    sync_gitops.sync("airbnb-inference", SHA, "isaiah1701/prod-mlops-platform")
    assert read.call_count == 3
    command = patch.call_args.args[0]
    assert json.loads(command[-1])["operation"]["sync"]["revision"] == SHA


def test_gitops_rejects_unexpected_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a selected application from deploying an unrelated repository."""
    monkeypatch.setattr(
        sync_gitops, "_application", Mock(return_value=application_status(SHA))
    )
    patch = Mock()
    monkeypatch.setattr(sync_gitops.subprocess, "run", patch)
    with pytest.raises(ValueError, match="source"):
        sync_gitops.sync("airbnb-inference", SHA, "unrelated/repository")
    patch.assert_not_called()


@pytest.mark.parametrize("version", ["1", "different"])
def test_release_checks_inference_matches_ready_model(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    """Accept matching champion inference and reject a mismatched model response."""
    responses = [
        io.BytesIO(b'{"status":"ready","alias":"champion","version":"1"}'),
        io.BytesIO(
            json.dumps(
                {
                    "predicted_price": 100.0,
                    "model_version": version,
                    "model_alias": "champion",
                    "prediction_id": "test",
                }
            ).encode()
        ),
    ]
    request = Mock(side_effect=responses)
    monkeypatch.setattr(verify_inference, "urlopen", request)
    if version == "different":
        with pytest.raises(ValueError, match="verification failed"):
            verify_inference.verify("https://serving.example")
    else:
        verify_inference.verify("https://serving.example")
    assert request.call_count == 2
