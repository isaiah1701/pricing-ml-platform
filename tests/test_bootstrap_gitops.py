"""Exercise bootstrap ownership, target safety and idempotence without a cluster."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from scripts import bootstrap_gitops as bootstrap

LOGGER = logging.getLogger(__name__)
ACCOUNT = "123456789012"
CLUSTER = {
    "name": "prod-mlops-prod",
    "status": "ACTIVE",
    "arn": f"arn:aws:eks:eu-west-2:{ACCOUNT}:cluster/prod-mlops-prod",
    "endpoint": "https://eks.test",
}
READY_NODE = {
    "metadata": {"labels": {"workload": "system"}},
    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
}
WORKLOAD = {
    "kind": "Deployment",
    "metadata": {"name": "argocd-server", "generation": 1},
    "spec": {"replicas": 1},
    "status": {"observedGeneration": 1, "readyReplicas": 1},
}


def helmfile_state() -> dict[str, Any]:
    """Read the actual declared Helmfile releases without invoking cloud tools."""
    return list(yaml.safe_load_all((bootstrap.ROOT / "helmfile.yaml").read_text()))[1]


@pytest.mark.parametrize(
    ("environment", "account", "region", "cluster"),
    [
        ("staging", ACCOUNT, "eu-west-2", CLUSTER),
        ("production", "999999999999", "eu-west-2", CLUSTER),
        ("production", ACCOUNT, "us-east-1", CLUSTER),
        ("production", ACCOUNT, "eu-west-2", {**CLUSTER, "status": "CREATING"}),
        ("production", ACCOUNT, "eu-west-2", {**CLUSTER, "arn": "wrong-cluster"}),
    ],
)
def test_wrong_bootstrap_target_is_rejected(
    environment: str,
    account: str,
    region: str,
    cluster: dict[str, Any],
) -> None:
    """Reject account, environment, region and inactive/wrong cluster mismatches."""
    with pytest.raises(ValueError):
        bootstrap.validate_target(
            environment,
            account,
            region,
            "prod-mlops-prod",
            {"Account": ACCOUNT},
            cluster,
        )


@pytest.mark.parametrize(
    "nodes",
    [[], [READY_NODE] * 3, [{"metadata": {"labels": {"workload": "training"}}}]],
)
def test_bootstrap_requires_existing_budget_capacity(
    nodes: list[dict[str, Any]],
) -> None:
    """Reject missing system capacity or a cluster already over the node budget."""
    with pytest.raises(ValueError):
        bootstrap.validate_nodes({"items": nodes})


def test_helmfile_cannot_take_over_downstream_releases() -> None:
    """Allow only the existing Argo release and existing budget values."""
    state = helmfile_state()
    assert bootstrap.validate_helmfile(state)["name"] == "argocd"
    copied = deepcopy(state)
    copied["releases"].append({"name": "cert-manager"})
    with pytest.raises(ValueError, match="only"):
        bootstrap.validate_helmfile(copied)


def test_healthy_existing_release_is_preserved_on_repeated_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip all Helm changes even if an existing healthy chart is older than the proposal."""
    runner = Mock(
        return_value='[{"name":"argocd","status":"deployed","chart":"argo-cd-older"}]'
    )
    monkeypatch.setattr(bootstrap, "_run", runner)
    monkeypatch.setattr(bootstrap, "_workloads", Mock(return_value=[WORKLOAD]))
    release = bootstrap.validate_helmfile(helmfile_state())
    for _ in range(2):
        assert "preserved" in bootstrap.establish_argocd(release)
    assert not any(call.args[0][0] == "helmfile" for call in runner.call_args_list)


def test_unmanaged_existing_argocd_requires_ownership_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never install a competing release over controllers with unknown ownership."""
    runner = Mock(return_value="[]")
    monkeypatch.setattr(bootstrap, "_run", runner)
    monkeypatch.setattr(bootstrap, "_workloads", Mock(return_value=[WORKLOAD]))
    with pytest.raises(ValueError, match="ownership"):
        bootstrap.establish_argocd(bootstrap.validate_helmfile(helmfile_state()))
    assert runner.call_count == 2


def test_fresh_install_uses_helmfile_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the single declared release without adding nodes or downstream controllers."""
    runner = Mock(side_effect=["", ""])
    monkeypatch.setattr(bootstrap, "_run", runner)
    monkeypatch.setattr(bootstrap, "_workloads", Mock(return_value=[]))
    bootstrap.establish_argocd(bootstrap.validate_helmfile(helmfile_state()))
    assert runner.call_args.args[0] == [
        "helmfile",
        "-e",
        "production",
        "--kube-context",
        "mlops-bootstrap",
        "sync",
    ]


def test_bootstrap_sequence_and_root_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the full bootstrap orchestration with existing healthy controllers and mocked tools."""
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], *, timeout: int = 180) -> str:
        """Return narrow fixture responses for all external bootstrap commands."""
        del timeout
        calls.append(arguments)
        if arguments[:3] == ["aws", "sts", "get-caller-identity"]:
            return json.dumps({"Account": ACCOUNT})
        if arguments[:3] == ["aws", "eks", "describe-cluster"]:
            return json.dumps({"cluster": CLUSTER})
        if arguments[:3] == ["kubectl", "config", "view"]:
            return json.dumps(
                {"clusters": [{"cluster": {"server": CLUSTER["endpoint"]}}]}
            )
        if arguments[:3] == ["kubectl", "get", "nodes"]:
            return json.dumps({"items": [READY_NODE]})
        if arguments[:3] == ["kubectl", "get", "namespace"]:
            return "namespace/argocd"
        if arguments[0] == "helmfile":
            return yaml.safe_dump(helmfile_state())
        if arguments[:2] == ["helm", "list"]:
            return '[{"status":"deployed","chart":"argo-cd-10.10.3"}]'
        if "deployments,statefulsets" in arguments:
            return json.dumps({"items": [WORKLOAD]})
        if "applications" in arguments:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "airbnb-platform"},
                            "spec": {"project": "prod-mlops"},
                        }
                    ]
                }
            )
        return ""

    monkeypatch.setattr(bootstrap, "_run", fake_run)
    root_sync = Mock()
    monkeypatch.setattr(bootstrap, "sync", root_sync)
    monkeypatch.setenv("GITHUB_REPOSITORY", "isaiah1701/prod-mlops-platform")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    bootstrap.bootstrap("production", ACCOUNT, "eu-west-2", "prod-mlops-prod")
    assert calls[0][:3] == ["aws", "sts", "get-caller-identity"]
    assert not any(
        arguments[0] == "helmfile" and arguments[-1] == "sync" for arguments in calls
    )
    root_sync.assert_called_once_with(
        "prod-mlops-bootstrap", "a" * 40, "isaiah1701/prod-mlops-platform", timeout=600
    )
    assert any(
        arguments[:3] == ["kubectl", "apply", "--server-side"] for arguments in calls
    )
    assert not any(
        "delete" in arguments or "destroy" in arguments for arguments in calls
    )


def test_workflow_inventory_is_exactly_eight_and_bootstrap_is_manual() -> None:
    """Keep the requested public entry points and all bootstrap approval/target gates explicit."""
    folder = bootstrap.ROOT / ".github/workflows"
    expected = {
        "ml-data",
        "ml-training",
        "ml-deploy",
        "docker",
        "terraform-plan",
        "terraform-apply",
        "bootstrap",
        "kubernetes",
    }
    assert {path.stem for path in folder.glob("*.yml")} == expected
    config = yaml.load((folder / "bootstrap.yml").read_text(), Loader=yaml.BaseLoader)
    assert config["name"] == "Bootstrap"
    assert set(config["on"]) == {"workflow_dispatch"}
    job = config["jobs"]["bootstrap"]
    assert "inputs.confirm_bootstrap" in job["if"] and "BOOTSTRAP_ENABLED" in job["if"]
    assert "refs/heads/main" in job["if"]
    assert job["environment"] == "${{ inputs.target_environment }}"
    assert config["concurrency"]["group"] == "kubernetes-production"
