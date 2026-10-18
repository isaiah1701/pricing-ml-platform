"""Bootstrap only the existing Argo CD control plane after strict EKS target checks."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from scripts.sync_gitops import sync

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "mlops-bootstrap"
CORE_WORKLOADS = (
    "deployment/coredns",
    "daemonset/aws-node",
    "daemonset/kube-proxy",
    "daemonset/eks-pod-identity-agent",
    "deployment/ebs-csi-controller",
)


def _run(arguments: list[str], *, timeout: int = 180) -> str:
    """Run a bounded command without dumping credentials, Secrets or full API responses."""
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        LOGGER.error(
            "Bootstrap command failed: %s %s (exit %d)",
            arguments[0],
            arguments[1] if len(arguments) > 1 else "",
            completed.returncode,
        )
        raise RuntimeError(
            "Bootstrap command failed; inspect the selected resource from an authorized workstation"
        )
    return completed.stdout


def validate_target(
    environment: str,
    account: str,
    region: str,
    name: str,
    identity: dict[str, Any],
    cluster: dict[str, Any],
) -> None:
    """Reject any account, region or cluster outside the explicit production target."""
    if (
        environment != "production"
        or region != "eu-west-2"
        or name != "prod-mlops-prod"
    ):
        raise ValueError(
            "This repository bootstrap supports production/prod-mlops-prod in eu-west-2 only"
        )
    if not re.fullmatch(r"\d{12}", account) or identity.get("Account") != account:
        raise ValueError(
            "Authenticated AWS account does not match EXPECTED_AWS_ACCOUNT_ID"
        )
    expected = f"arn:aws:eks:{region}:{account}:cluster/{name}"
    if (
        cluster.get("arn") != expected
        or cluster.get("name") != name
        or cluster.get("status") != "ACTIVE"
    ):
        raise ValueError(
            "EKS cluster identity/status does not match the intended target"
        )
    LOGGER.info("Validated production EKS target %s in %s", name, region)


def validate_nodes(nodes: dict[str, Any]) -> None:
    """Require existing schedulable system capacity without provisioning another node."""
    items = nodes.get("items", [])
    if not items or len(items) > 2:
        raise ValueError(
            "Bootstrap requires one or two existing workers; it never creates nodes"
        )
    ready = [
        node
        for node in items
        if not node.get("spec", {}).get("unschedulable", False)
        and node.get("metadata", {}).get("labels", {}).get("workload") == "system"
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
    ]
    if not ready:
        raise ValueError("No Ready schedulable workload=system worker is available")
    LOGGER.info(
        "Confirmed %d existing worker(s), including Ready system capacity", len(items)
    )


def validate_helmfile(state: dict[str, Any]) -> dict[str, Any]:
    """Keep Helmfile ownership limited to the pre-existing Argo CD Helm release."""
    releases = state.get("releases", [])
    if len(releases) != 1:
        raise ValueError("Bootstrap Helmfile must own only the Argo CD release")
    release = releases[0]
    if (
        release.get("name") != "argocd"
        or release.get("namespace") != "argocd"
        or release.get("chart") != "argo/argo-cd"
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(release.get("version", "")))
    ):
        raise ValueError(
            "Unexpected Argo CD release identity or unpinned chart version"
        )
    values = release.get("values", [])
    expected = ROOT / "serving/argocd/values-budget.yaml"
    if (
        len(values) != 1
        or not isinstance(values[0], str)
        or (ROOT / values[0]).resolve() != expected
    ):
        raise ValueError(
            "Reuse the existing Argo CD budget values without duplicate release definitions"
        )
    return release


def _workloads() -> list[dict[str, Any]]:
    """Read Argo-owned workload summaries without querying Secret contents."""
    return json.loads(
        _run(
            [
                "kubectl",
                "-n",
                "argocd",
                "get",
                "deployments,statefulsets",
                "-l",
                "app.kubernetes.io/instance=argocd",
                "-o",
                "json",
            ]
        )
    )["items"]


def _healthy(workloads: list[dict[str, Any]]) -> bool:
    """Require observed controller generations and all requested replicas ready."""
    return bool(workloads) and all(
        item.get("status", {}).get("observedGeneration", 0)
        >= item["metadata"].get("generation", 1)
        and item.get("status", {}).get("readyReplicas", 0)
        >= item.get("spec", {}).get("replicas", 1)
        for item in workloads
    )


def establish_argocd(release: dict[str, Any], *, upgrade: bool = False) -> str:
    """Install missing Argo CD, repair the pinned release, and preserve healthy existing installs."""
    namespace = _run(
        ["kubectl", "get", "namespace", "argocd", "--ignore-not-found", "-o", "name"]
    )
    if not namespace.strip():
        _run(
            ["helmfile", "-e", "production", "--kube-context", CONTEXT, "sync"],
            timeout=900,
        )
        return "Argo CD installed through Helmfile"
    installed = json.loads(
        _run(
            [
                "helm",
                "list",
                "-n",
                "argocd",
                "--all",
                "--filter",
                "^argocd$",
                "-o",
                "json",
            ]
        )
    )
    workloads = _workloads()
    if (
        installed
        and installed[0].get("status") == "deployed"
        and _healthy(workloads)
        and not upgrade
    ):
        LOGGER.info(
            "Existing healthy %s preserved; no Helm upgrade requested",
            installed[0].get("chart"),
        )
        return "existing healthy Argo CD preserved"
    if not installed and workloads:
        raise ValueError(
            "Argo CD exists without the expected Helm ownership; review migration before installing"
        )
    if (
        installed
        and not upgrade
        and installed[0].get("chart") != f"argo-cd-{release['version']}"
    ):
        raise ValueError(
            "Existing Argo CD differs from the pinned chart; explicit upgrade approval is required"
        )
    _run(
        ["helmfile", "-e", "production", "--kube-context", CONTEXT, "sync"], timeout=900
    )
    return "Argo CD installed or reconciled through Helmfile"


def _diagnostics() -> None:
    """Report bounded readiness counts rather than logs or sensitive Kubernetes objects."""
    try:
        for item in _workloads():
            LOGGER.error(
                "Argo readiness: %s %s ready=%s desired=%s",
                item["kind"],
                item["metadata"]["name"],
                item.get("status", {}).get("readyReplicas", 0),
                item.get("spec", {}).get("replicas", 1),
            )
    except (RuntimeError, subprocess.TimeoutExpired, ValueError):
        LOGGER.error("Cannot read Argo readiness diagnostics; check cluster access")


def bootstrap(
    environment: str, account: str, region: str, name: str, *, upgrade: bool = False
) -> None:
    """Validate the target, establish Argo CD and reconcile only the root GitOps application."""
    identity = json.loads(
        _run(["aws", "sts", "get-caller-identity", "--output", "json"])
    )
    cluster = json.loads(
        _run(
            [
                "aws",
                "eks",
                "describe-cluster",
                "--name",
                name,
                "--region",
                region,
                "--output",
                "json",
            ]
        )
    )["cluster"]
    validate_target(environment, account, region, name, identity, cluster)
    _run(
        [
            "aws",
            "eks",
            "update-kubeconfig",
            "--name",
            name,
            "--region",
            region,
            "--alias",
            CONTEXT,
        ]
    )
    configured = json.loads(
        _run(["kubectl", "config", "view", "--minify", "-o", "json"])
    )
    if configured["clusters"][0]["cluster"]["server"] != cluster["endpoint"]:
        raise ValueError("Kubeconfig server differs from the validated EKS endpoint")
    validate_nodes(json.loads(_run(["kubectl", "get", "nodes", "-o", "json"])))
    for resource in CORE_WORKLOADS:
        _run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "rollout",
                "status",
                resource,
                "--timeout=120s",
            ],
            timeout=140,
        )
    LOGGER.info(
        "Terraform-managed EKS foundational add-ons are ready; ownership unchanged"
    )
    state = yaml.safe_load(_run(["helmfile", "-e", environment, "build"]))
    release = validate_helmfile(state)
    # Validate root references before making any Helm/Kubernetes changes.
    root_path = ROOT / "serving/argocd/root-application.yaml"
    root = yaml.safe_load(root_path.read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    if (
        root.get("kind") != "Application"
        or root["metadata"]["name"] != "prod-mlops-bootstrap"
        or root["metadata"]["namespace"] != "argocd"
        or root["spec"]["destination"]
        != {"server": "https://kubernetes.default.svc", "namespace": "argocd"}
        or root["spec"]["source"].get("targetRevision") != "main"
        or root["spec"]["source"]["repoURL"].removesuffix(".git")
        != f"https://github.com/{repository}"
        or root["spec"]["source"]["path"] != "serving/helm"
        or root["spec"]["source"]["helm"]["valueFiles"] != ["values/bootstrap.yaml"]
    ):
        raise ValueError(
            "Root application does not reference this repository's existing bootstrap chart"
        )
    try:
        result = establish_argocd(release, upgrade=upgrade)
        for resource in _workloads():
            kind = "deployment" if resource["kind"] == "Deployment" else "statefulset"
            _run(
                [
                    "kubectl",
                    "-n",
                    "argocd",
                    "rollout",
                    "status",
                    f"{kind}/{resource['metadata']['name']}",
                    "--timeout=300s",
                ],
                timeout=320,
            )
        if not _healthy(_workloads()):
            raise RuntimeError("Argo CD controllers are not Ready after bootstrap")
        _run(
            [
                "kubectl",
                "apply",
                "--server-side",
                "--field-manager=mlops-bootstrap",
                "-f",
                str(root_path),
            ]
        )
        sync(
            root["metadata"]["name"], os.environ["GITHUB_SHA"], repository, timeout=600
        )
        applications = json.loads(
            _run(["kubectl", "-n", "argocd", "get", "applications", "-o", "json"])
        )["items"]
        children = [
            item
            for item in applications
            if item.get("spec", {}).get("project") == "prod-mlops"
        ]
        if not children:
            raise RuntimeError(
                "Root sync did not create the expected downstream applications"
            )
        for application in children:
            if any(
                condition.get("type") in {"ComparisonError", "InvalidSpecError"}
                for condition in application.get("status", {}).get("conditions", [])
            ):
                raise RuntimeError(
                    "A downstream application cannot reconcile; inspect its source prerequisites"
                )
        pending = [
            item["metadata"]["name"]
            for item in children
            if item.get("status", {}).get("sync", {}).get("status") != "Synced"
        ]
        LOGGER.info(
            "Bootstrap complete: %s; %d downstream applications registered",
            result,
            len(children),
        )
        if pending:
            LOGGER.info(
                "Downstream applications awaiting manual Kubernetes reconciliation: %s",
                ", ".join(pending),
            )
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a") as output:
                output.write(
                    f"Bootstrap: {result}. Root synced at `{os.environ['GITHUB_SHA']}`.\n\n"
                )
                output.write(
                    "Pending downstream manual reconciliation: "
                    + (", ".join(pending) or "none")
                    + ".\n"
                )
    except Exception:
        _diagnostics()
        raise


def main() -> None:
    """Read the explicitly approved target; never infer an account from credentials alone."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    bootstrap(
        os.environ["BOOTSTRAP_ENVIRONMENT"],
        os.environ["EXPECTED_AWS_ACCOUNT_ID"],
        os.environ["AWS_REGION"],
        os.environ["EKS_CLUSTER_NAME"],
        upgrade=os.environ.get("BOOTSTRAP_UPGRADE_ARGOCD") == "true",
    )


if __name__ == "__main__":
    main()
