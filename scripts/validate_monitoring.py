"""Check dashboard provisioning and metric names; emit rules for real PromQL validation."""

from __future__ import annotations

import argparse
import ast
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def metric_names(root: Path) -> set[str]:
    """Extract declared application metrics and histogram series from their producers."""
    names: set[str] = set()
    for relative in (
        "serving/app/api.py",
        "monitoring/drift_check.py",
        "monitoring/model_quality.py",
        "monitoring/operational_metrics.py",
    ):
        path = root / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                bound = {item.id for item in ast.walk(node.target) if isinstance(item, ast.Name)}
                if any(
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in {"Gauge", "Counter", "Histogram"}
                    and call.args and isinstance(call.args[0], ast.Name)
                    and call.args[0].id in bound
                    for statement in node.body for call in ast.walk(statement)
                ):
                    names.update(
                        value.value for value in ast.walk(node.iter)
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and value.value.startswith("airbnb_")
                    )
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "METRIC_NAMES" for target in targets):
                    names.update(
                        value.value for value in ast.walk(node.value)
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and value.value.startswith("airbnb_")
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"Gauge", "Counter", "Histogram"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                name = node.args[0].value
                names.add(name)
                if node.func.id == "Histogram":
                    names.update(
                        f"{name}_{suffix}" for suffix in ("bucket", "count", "sum")
                    )
    bootstrap = yaml.safe_load((root / "serving/helm/values/bootstrap.yaml").read_text())
    custom = bootstrap["addons"]["monitoring"]["values"]["kube-state-metrics"]["customResourceState"]
    if custom.get("enabled"):
        for resource in custom["config"]["spec"]["resources"]:
            prefix = resource.get("metricNamePrefix", "kube_customresource")
            names.update(f"{prefix}_{metric['name']}" for metric in resource["metrics"])
    return names


def validate_dashboards(root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    """Validate copies, datasource IDs and application metric mappings without a cluster."""
    LOGGER.info("Validating dashboard provisioning and application metric contracts")
    declared = metric_names(root)
    source = root / "monitoring/grafana/dashboards"
    chart = root / "serving/helm/files/dashboards"
    if {path.name for path in source.glob("*.json")} != {
        path.name for path in chart.glob("*.json")
    }:
        raise ValueError("Source and Helm dashboard file sets differ")
    provision = yaml.safe_load(
        (
            root / "monitoring/grafana/provisioning/datasources/prometheus.yml"
        ).read_text()
    )
    datasource_uids = {item["uid"] for item in provision["datasources"]}
    rules: list[dict[str, Any]] = []
    uids: set[str] = set()
    for path in sorted(source.glob("*.json")):
        dashboard = json.loads(path.read_text())
        if dashboard != json.loads((chart / path.name).read_text()):
            raise ValueError(f"Helm dashboard differs: {path.name}")
        uid = dashboard["uid"]
        if uid in uids:
            raise ValueError("Duplicate dashboard UID")
        uids.add(uid)
        ids: set[int] = set()
        for panel in dashboard["panels"]:
            if panel["id"] in ids:
                raise ValueError(f"Duplicate panel ID in {uid}")
            ids.add(panel["id"])
            if panel["datasource"]["uid"] not in datasource_uids:
                raise ValueError(f"Unknown datasource in {uid}")
            refs: set[str] = set()
            for target in panel.get("targets", []):
                if target["refId"] in refs:
                    raise ValueError(f"Duplicate target reference in {uid}")
                refs.add(target["refId"])
                expression = target["expr"]
                metric_expression = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expression)
                used = set(re.findall(r"\b(?:airbnb|mlops)_[a-z0-9_]+", metric_expression))
                if used - declared:
                    raise ValueError(
                        f"Undefined application metrics: {sorted(used - declared)}"
                    )
                rules.append(
                    {
                        "record": f"dashboard_{uid.replace('-', '_')}_{panel['id']}_{target['refId']}",
                        "expr": expression,
                    }
                )
    if not rules:
        raise ValueError("No dashboard expressions found")
    LOGGER.info(
        "Validated %d dashboards and %d expression mappings", len(uids), len(rules)
    )
    return rules


def main() -> None:
    """Optionally emit recording rules for promtool syntax and synthetic-series checks."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules-output", type=Path)
    args = parser.parse_args()
    try:
        rules = validate_dashboards()
    except (ValueError, KeyError) as exc:
        LOGGER.error("Monitoring validation failed: %s", exc)
        raise
    if args.rules_output:
        args.rules_output.parent.mkdir(parents=True, exist_ok=True)
        args.rules_output.write_text(
            yaml.safe_dump(
                {"groups": [{"name": "dashboard-validation", "rules": rules}]}
            )
        )


if __name__ == "__main__":
    main()
