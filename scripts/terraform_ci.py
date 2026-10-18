"""Create and consume an exact reviewed Terraform plan with checked provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1] / "terraform/environments/prod"


def _digest(path: Path) -> str:
    """Hash an artifact or provider lockfile without displaying its contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backend_configuration() -> dict[str, Any]:
    """Require the existing backend explicitly so CI cannot create a different state."""
    configuration = {
        key: os.environ.get(environment, "").strip()
        for key, environment in (
            ("bucket", "TF_STATE_BUCKET"),
            ("key", "TF_STATE_KEY"),
            ("region", "TF_STATE_REGION"),
        )
    }
    if not all(configuration.values()):
        LOGGER.error("Terraform state bucket, key and region must be configured")
        raise ValueError("Configure TF_STATE_BUCKET, TF_STATE_KEY and TF_STATE_REGION")
    return {**configuration, "encrypt": True, "use_lockfile": True}


def backend_identity(configuration: dict[str, Any]) -> str:
    """Bind approval to the backend configuration without putting it in the artifact."""
    return hashlib.sha256(
        json.dumps(configuration, sort_keys=True).encode()
    ).hexdigest()


def _private_write(path: Path, text: str) -> None:
    """Store backend, inputs or runner logs with owner-only permissions."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)
    path.chmod(0o600)


def _run(arguments: list[str], runtime: Path) -> str:
    """Run Terraform while retaining potentially sensitive output only on the runner."""
    result = subprocess.run(
        ["terraform", f"-chdir={ROOT}", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log = runtime / "terraform-error.log"
        _private_write(log, result.stdout + result.stderr)
        LOGGER.error(
            "Terraform %s failed with exit %d; private runner log: %s",
            arguments[0],
            result.returncode,
            log,
        )
        raise RuntimeError("Terraform failed; inspect its private runner log")
    return result.stdout


def expected_account() -> str:
    """Bind infrastructure operations to the explicitly configured AWS account."""
    expected = os.environ.get("EXPECTED_AWS_ACCOUNT_ID", "")
    if not re.fullmatch(r"[0-9]{12}", expected):
        raise ValueError(
            "Configure EXPECTED_AWS_ACCOUNT_ID before planning or applying"
        )
    identity = json.loads(
        subprocess.check_output(
            ["aws", "sts", "get-caller-identity", "--output", "json"], text=True
        )
    )
    if identity.get("Account") != expected:
        raise ValueError(
            "Authenticated AWS account differs from the reviewed production target"
        )
    return expected


def verify_plan(
    directory: Path,
    *,
    repository: str,
    source_sha: str,
    run_id: str,
    backend: dict[str, Any],
    terraform_version: str,
    lockfile: Path,
    account: str,
) -> dict[str, Any]:
    """Reject mismatched, expired, corrupted or differently configured plans before init/apply."""
    metadata = json.loads((directory / "metadata.json").read_text())
    expected = {
        "schema_version": 1,
        "environment": "production",
        "aws_account": account,
        "repository": repository,
        "source_sha": source_sha,
        "run_id": run_id,
        "backend_sha256": backend_identity(backend),
        "terraform_version": terraform_version,
        "lock_sha256": _digest(lockfile),
        "plan_sha256": _digest(directory / "tfplan"),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        LOGGER.error("Terraform plan provenance or checksum does not match approval")
        raise ValueError("Terraform plan provenance/checksum mismatch")
    created = datetime.fromisoformat(metadata["created_at"])
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age < -300 or age > 86400:
        raise ValueError("Terraform plan has expired or has an invalid creation time")
    LOGGER.info("Verified reviewed Terraform plan for commit %s", source_sha)
    return metadata


def execute(operation: str, directory: Path) -> None:
    """Plan from explicit production inputs or apply the verified saved binary without replanning."""
    backend = backend_configuration()
    account = expected_account()
    runtime = Path(os.environ["RUNNER_TEMP"]) / "mlops-terraform-runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    directory = directory.resolve()
    version = json.loads(_run(["version", "-json"], runtime))["terraform_version"]
    lockfile = ROOT / ".terraform.lock.hcl"
    if operation == "apply":
        verify_plan(
            directory,
            repository=os.environ["GITHUB_REPOSITORY"],
            source_sha=os.environ["PLAN_SOURCE_SHA"],
            run_id=os.environ["PLAN_RUN_ID"],
            backend=backend,
            terraform_version=version,
            lockfile=lockfile,
            account=account,
        )
    elif operation != "plan":
        raise ValueError("Unsupported Terraform operation")
    inputs = runtime / "prod.tfvars.json"
    if operation == "plan":
        try:
            values = json.loads(os.environ.get("TF_PROD_TFVARS_JSON", ""))
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Configure TF_PROD_TFVARS_JSON with the actual production inputs"
            ) from exc
        if (
            not isinstance(values, dict)
            or values.get("cost_optimized") is not True
            or values.get("aws_region", "eu-west-2") != "eu-west-2"
        ):
            raise ValueError(
                "Production CI requires explicit inputs with cost_optimized=true"
            )
        _private_write(inputs, json.dumps(values))
    backend_file = runtime / "backend.json"
    _private_write(backend_file, json.dumps(backend))
    _run(
        [
            "init",
            "-input=false",
            "-lockfile=readonly",
            f"-backend-config={backend_file}",
        ],
        runtime,
    )
    if operation == "apply":
        LOGGER.info("Applying the approved binary plan; no new plan will be generated")
        _run(["apply", "-input=false", str(directory / "tfplan")], runtime)
        LOGGER.info("Approved Terraform apply completed")
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _run(["validate"], runtime)
    _run(
        [
            "plan",
            "-input=false",
            "-lock-timeout=5m",
            f"-var-file={inputs}",
            f"-out={directory / 'tfplan'}",
        ],
        runtime,
    )
    description = json.loads(
        _run(["show", "-json", str(directory / "tfplan")], runtime)
    )
    metadata = {
        "schema_version": 1,
        "environment": "production",
        "aws_account": account,
        "repository": os.environ["GITHUB_REPOSITORY"],
        "source_sha": os.environ["GITHUB_SHA"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "backend_sha256": backend_identity(backend),
        "terraform_version": version,
        "lock_sha256": _digest(lockfile),
        "plan_sha256": _digest(directory / "tfplan"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _private_write(directory / "metadata.json", json.dumps(metadata, indent=2))
    changes = [
        {"address": item["address"], "actions": item["change"]["actions"]}
        for item in description.get("resource_changes", [])
        if item["change"]["actions"] != ["no-op"]
    ]
    (directory / "changes.json").write_text(json.dumps(changes, indent=2))
    LOGGER.info(
        "Saved Terraform plan with %d resource changes; source commit %s",
        len(changes),
        metadata["source_sha"],
    )
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write(
                f"Terraform Plan: commit `{metadata['source_sha']}`, run `{metadata['run_id']}`.\n\n"
            )
            output.write("| Resource | Actions |\n| --- | --- |\n")
            output.writelines(
                f"| `{item['address']}` | {', '.join(item['actions'])} |\n"
                for item in changes
            )


def main() -> None:
    """Read workflow inputs without logging tfvars, state contents or plan values."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "apply"])
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()
    execute(args.operation, args.artifact_dir)


if __name__ == "__main__":
    main()
