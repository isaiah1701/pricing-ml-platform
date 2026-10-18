"""Trigger an approved Airflow 3 DAG and wait for its terminal state."""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
ALLOWED_DAGS = {"airbnb_feature_build", "airbnb_model_training", "airbnb_model_deploy"}


def run_pipeline(
    url: str,
    token: str,
    dag: str,
    run_id: str,
    *,
    timeout: float = 21000,
    conf: dict[str, object] | None = None,
) -> None:
    """Submit one uniquely identified run, failing on rejection or timeout."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        LOGGER.error("Airflow API requires an HTTPS URL without embedded credentials")
        raise ValueError("Configure AIRFLOW_API_URL with a reachable HTTPS endpoint")
    if not token or dag not in ALLOWED_DAGS or not run_id:
        LOGGER.error("Airflow pipeline configuration is incomplete or disallowed")
        raise ValueError("A token, allowed DAG and unique run ID are required")
    endpoint = f"{url.rstrip('/')}/api/v2/dags/{quote(dag, safe='')}/dagRuns"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = Request(
        endpoint,
        data=json.dumps(
            {"dag_run_id": run_id, "logical_date": None, "conf": conf or {}}
        ).encode(),
        headers=headers,
        method="POST",
    )
    LOGGER.info("Submitting approved DAG %s with run ID %s", dag, run_id)
    with urlopen(request, timeout=30) as response:
        response.read()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = Request(f"{endpoint}/{quote(run_id, safe='')}", headers=headers)
        with urlopen(request, timeout=30) as response:
            state = json.load(response)["state"]
        if state == "success":
            LOGGER.info("DAG %s completed successfully", dag)
            return
        if state == "failed":
            LOGGER.error("DAG %s failed; inspect Airflow task logs", dag)
            raise RuntimeError("Airflow pipeline failed")
        time.sleep(15)
    LOGGER.error("Timed out waiting for DAG %s; it may still be running", dag)
    raise TimeoutError("Inspect Airflow before retrying; the DAG was not cancelled")


def main() -> None:
    """Read workflow configuration without logging API credentials."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_pipeline(
        os.environ.get("AIRFLOW_API_URL", ""),
        os.environ.get("AIRFLOW_API_TOKEN", ""),
        os.environ.get("AIRFLOW_DAG", ""),
        os.environ.get("AIRFLOW_RUN_ID", ""),
        conf=json.loads(os.environ.get("AIRFLOW_DAG_CONF", "{}")),
    )


if __name__ == "__main__":
    main()
