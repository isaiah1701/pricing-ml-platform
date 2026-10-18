"""Serialize verified model promotions and verify serving before completing rollback."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import mlflow
import requests
from mlflow import MlflowClient

from serving.app.model_loader import _require_registry_provenance
from training.provenance import sha256_directory
from training.recovery import persist_if_configured
from training.registry import tag_version_before_alias

LOGGER = logging.getLogger(__name__)


def verify_registry_version(client: Any, name: str, version: str) -> Any:
    """Validate run provenance and exact retained artifact bytes before alias mutation."""
    selected = client.get_model_version(name, version)
    if selected.status != "READY":
        raise ValueError("Selected model artifacts are not READY")
    run = client.get_run(selected.run_id)
    if (
        run.info.status != "FINISHED"
        or run.data.tags.get("artifact_reload_verified") != "true"
    ):
        raise ValueError(
            "Selected source run has not passed artifact reload verification"
        )
    LOGGER.info("Verifying model %s version %s", name, version)
    mlflow.set_tracking_uri(client.tracking_uri)
    _require_registry_provenance(selected)
    with tempfile.TemporaryDirectory(prefix="approved-deploy-") as temporary:
        bundle_path = client.download_artifacts(
            selected.run_id, "provenance/release_bundle.json", temporary
        )
        bundle = json.loads(Path(bundle_path).read_text())
        artifact = mlflow.artifacts.download_artifacts(
            artifact_uri=bundle["model_uri"],
            tracking_uri=client.tracking_uri,
            dst_path=str(Path(temporary) / "model"),
        )
        if sha256_directory(artifact) != selected.tags["model_artifact_sha256"]:
            LOGGER.error("Selected model checksum does not match approved provenance")
            raise ValueError("Approved model artifact checksum mismatch")
    return selected


def _paths(name: str) -> tuple[Path, Path]:
    """Locate shared per-registry model receipts on the existing Airflow PVC."""
    root = Path(
        os.getenv("AIRBNB_PROMOTION_STATE_DIR", "data/training-checkpoints/promotions")
    )
    root.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(name.encode()).hexdigest()
    return root / f"{identity}.lock", root / f"{identity}.json"


@contextmanager
def _locked(name: str) -> Iterator[Path]:
    """Bound mutation lock acquisition and serialize cooperating registry writers."""
    lock, receipt = _paths(name)
    with lock.open("a") as handle:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Another model promotion holds the mutation lock"
                    )
                time.sleep(0.1)
        try:
            yield receipt
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _save(path: Path, receipt: dict[str, Any]) -> None:
    """Durably replace operation state before the next external mutation."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(receipt, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path: Path) -> dict[str, Any]:
    """Read a durable promotion receipt or return an empty initial state."""
    return json.loads(path.read_text()) if path.exists() else {}


def _champion(client: Any, name: str, alias: str) -> str:
    """Resolve a concrete current alias target for stale-operation checks."""
    return str(client.get_model_version_by_alias(name, alias).version)


def _event(event: str, operation: str) -> bool:
    """Try publishing a durable event without changing a verified registry outcome."""
    from monitoring.operational_metrics import publish_model_event

    try:
        publish_model_event(event, f"{operation}:{event}")
        return True
    except Exception:
        LOGGER.exception(
            "Model telemetry publication failed; receipt retains its pending event"
        )
        return False


def _queue_event(receipt: dict[str, Any], event: str) -> list[dict[str, str]]:
    """Retain unsent events with their original IDs across later operations."""
    pending = list(receipt.get("event_pending", []))
    entry = {"event": event, "operation_id": receipt["operation_id"]}
    if entry not in pending:
        pending.append(entry)
    return pending


def _publish_pending(path: Path, receipt: dict[str, Any]) -> None:
    """Retry the receipt outbox and clear entries only after durable publication."""
    for entry in list(receipt.get("event_pending", [])):
        if _event(entry["event"], entry["operation_id"]):
            receipt["event_pending"].remove(entry)
            _save(path, receipt)


def _publish_quality(client: Any, name: str, version: str) -> None:
    """Expose actual approved run holdout metrics without inventing missing scores."""
    from monitoring.operational_metrics import publish_offline_evaluation

    try:
        selected = client.get_model_version(name, version)
        metrics = client.get_run(selected.run_id).data.metrics
        publish_offline_evaluation("champion", metrics)
    except Exception:
        LOGGER.exception(
            "Verified model remains selected; offline metric publication failed"
        )


def begin_promotion(
    client: Any,
    name: str,
    version: str,
    *,
    operation_id: str,
    approved_by: str,
    approved_at: str,
    production_alias: str = "champion",
    rollback_alias: str = "rollback",
    require_approved: bool = False,
    expected_previous: str = "",
) -> dict[str, Any]:
    """Validate artifacts, persist rollback intent, and promote under a shared lease."""
    if (
        not version.isdigit()
        or int(version) < 1
        or not operation_id
        or not approved_by
        or not approved_at
    ):
        raise ValueError("A concrete version and recorded approval are required")
    with _locked(name) as path:
        old = _read(path)
        if old and old["operation_id"] == operation_id:
            if old["promoted_version"] != version:
                raise RuntimeError("Operation ID cannot select a different model")
            if old["status"] in {"completed", "rolled_back"}:
                return old
            receipt = old
        else:
            if old and old["status"] not in {"completed", "rolled_back"}:
                raise RuntimeError(
                    "Unfinished promotion blocks new operations; recover its saved receipt first"
                )
            selected = verify_registry_version(client, name, version)
            previous = _champion(client, name, production_alias)
            if expected_previous and previous != expected_previous:
                raise RuntimeError(
                    "Champion changed since evaluation; approval must be repeated"
                )
            if (
                require_approved
                and version != previous
                and selected.tags.get("promotion_status") != "approved"
            ):
                raise ValueError(
                    "Use the governed training/promotion gates before selecting this version"
                )
            # Also verify the saved rollback candidate before committing any mutation.
            if previous != version:
                verify_registry_version(client, name, previous)
            receipt = {
                "operation_id": operation_id,
                "model_name": name,
                "previous_version": previous,
                "promoted_version": version,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "started_at": time.time(),
                "deadline": time.time() + 900,
                "status": "prepared",
                "production_alias": production_alias,
                "event_pending": list(old.get("event_pending", [])),
            }
            _save(path, receipt)
        observed = _champion(client, name, production_alias)
        if observed not in {receipt["previous_version"], version}:
            raise RuntimeError(
                "Champion changed outside this promotion; refusing stale mutation"
            )
        if receipt["status"] in {"rollback_pending", "rollback_failed"}:
            raise RuntimeError(
                "Interrupted rollback must be completed before promotion"
            )
        if time.time() > receipt["deadline"]:
            raise RuntimeError(
                "Promotion lease expired; restore its saved previous version before retrying"
            )
        if observed != version:
            client.set_registered_model_alias(
                name, rollback_alias, receipt["previous_version"]
            )
            tag_version_before_alias(
                client,
                name,
                version,
                production_alias,
                {
                    "promotion_status": "approved",
                    "approved_by": approved_by,
                    "approved_at": approved_at,
                    "previous_production_version": receipt["previous_version"],
                    "promotion_operation_id": operation_id,
                },
            )
        receipt["status"] = "promoted"
        _save(path, receipt)
        LOGGER.info(
            "Promotion %s selected model version %s; serving validation pending",
            operation_id,
            version,
        )
        return receipt


def complete_promotion(
    client: Any, promotion: dict[str, Any], *, serving_verified: bool
) -> None:
    """Finish only a still-current, serving-verified operation and persist its backup."""
    name = promotion["model_name"]
    with _locked(name) as path:
        receipt = _read(path)
        if receipt.get("operation_id") != promotion["operation_id"]:
            raise RuntimeError("A newer promotion owns this receipt")
        if receipt["status"] == "rolled_back":
            raise RuntimeError("A rolled-back promotion cannot complete")
        if (
            not serving_verified
            or _champion(client, name, receipt["production_alias"])
            != receipt["promoted_version"]
        ):
            raise RuntimeError(
                "Promotion cannot complete without current serving verification"
            )
        if receipt["status"] != "completed" and time.time() > receipt["deadline"]:
            raise RuntimeError(
                "Promotion validation exceeded its lease; rollback required"
            )
        persist_if_configured(client, name, receipt["production_alias"])
        receipt.update(
            status="completed",
            completed_at=time.time(),
            event_pending=_queue_event(receipt, "promotion"),
        )
        _save(path, receipt)
        _publish_pending(path, receipt)
        _publish_quality(client, name, receipt["promoted_version"])
        LOGGER.info(
            "Promotion %s completed after serving verification", receipt["operation_id"]
        )


def rollback_promotion_verified(
    client: Any,
    promotion: dict[str, Any],
    *,
    reason: str,
    verifier: Callable[[str], None],
    production_alias: str = "champion",
) -> str:
    """Restore a saved alias and verify serving before reporting rollback success."""
    name = promotion["model_name"]
    with _locked(name) as path:
        receipt = _read(path)
        if receipt.get("operation_id") != promotion["operation_id"]:
            raise RuntimeError(
                "A newer promotion owns this receipt; stale rollback refused"
            )
        if receipt["status"] == "completed":
            raise RuntimeError(
                "Completed promotion requires a separately approved rollback"
            )
        previous = receipt["previous_version"]
        current = _champion(client, name, production_alias)
        if current not in {previous, receipt["promoted_version"]}:
            raise RuntimeError(
                "Champion changed outside this operation; stale rollback refused"
            )
        if receipt["status"] == "rolled_back":
            _publish_pending(path, receipt)
            return previous
        verify_registry_version(client, name, previous)
        receipt.update(
            status="rollback_pending",
            rollback_reason=reason,
            rollback_started_at=time.time(),
        )
        _save(path, receipt)
        try:
            client.set_registered_model_alias(name, production_alias, previous)
        except Exception:
            LOGGER.exception(
                "Registry alias restoration failed; saved rollback target retained"
            )
            receipt.update(
                status="rollback_failed",
                rollback_failed_at=time.time(),
                event_pending=_queue_event(receipt, "rollback_failure"),
            )
            _save(path, receipt)
            _publish_pending(path, receipt)
            raise
    # Keep the durable lease while releasing flock during bounded network polling.
    try:
        verifier(previous)
        with _locked(name) as path:
            receipt = _read(path)
            if (
                receipt.get("operation_id") != promotion["operation_id"]
                or _champion(client, name, production_alias) != previous
            ):
                raise RuntimeError("Champion changed during rollback verification")
            if previous != promotion["promoted_version"]:
                client.set_model_version_tag(
                    name,
                    promotion["promoted_version"],
                    "promotion_status",
                    "rolled_back",
                )
            client.set_model_version_tag(
                name, promotion["promoted_version"], "rollback_reason", reason
            )
            client.set_model_version_tag(
                name,
                promotion["promoted_version"],
                "rollback_verified_at",
                str(time.time()),
            )
            persist_if_configured(client, name, production_alias)
            receipt.update(
                status="rolled_back",
                rollback_verified_at=time.time(),
                event_pending=_queue_event(receipt, "rollback"),
            )
            _save(path, receipt)
            _publish_pending(path, receipt)
            _publish_quality(client, name, previous)
        LOGGER.warning("Rollback verified at serving version %s", previous)
        return previous
    except Exception:
        with _locked(name) as path:
            receipt = _read(path)
            if receipt.get("operation_id") == promotion["operation_id"]:
                receipt.update(
                    status="rollback_failed",
                    rollback_failed_at=time.time(),
                    event_pending=_queue_event(receipt, "rollback_failure"),
                )
                _save(path, receipt)
                _publish_pending(path, receipt)
        LOGGER.exception("Alias restoration did not complete verified serving rollback")
        raise


def verify_serving_version(version: str, url: str | None = None) -> None:
    """Poll readiness and valid concrete-version predictions within a bounded window."""
    payload = {
        "neighbourhood_group": "Manhattan",
        "neighbourhood": "Midtown",
        "latitude": 40.758,
        "longitude": -73.9855,
        "room_type": "Entire home/apt",
        "minimum_nights": 2,
        "number_of_reviews": 10,
        "reviews_per_month": 1.25,
        "calculated_host_listings_count": 1,
        "availability_365": 120,
        "has_reviews": 1,
        "days_since_last_review": 30,
        "minimum_nights_band": "short",
        "availability_band": "medium",
        "location_cell": "40.76_-73.99",
        "distance_to_midtown_km": 0.0,
    }

    endpoint = (
        url or os.getenv("SERVING_URL", "http://airbnb-price-predictor")
    ).rstrip("/")
    deadline = time.monotonic() + 180
    while True:
        try:
            ready = requests.get(f"{endpoint}/ready", timeout=3)
            ready.raise_for_status()
            readiness = ready.json()
            if (
                readiness.get("status") != "ready"
                or readiness.get("alias") != "champion"
                or str(readiness.get("version")) != version
            ):
                raise ValueError("Serving has not loaded the required concrete model")
            latencies: list[float] = []
            for _ in range(20):
                started = time.monotonic()
                response = requests.post(f"{endpoint}/predict", json=payload, timeout=3)
                latencies.append(time.monotonic() - started)
                response.raise_for_status()
                body = response.json()
                price = float(body["predicted_price"])
                if (
                    type(body["predicted_price"]) not in {int, float}
                    or not 0 <= price < float("inf")
                    or str(body.get("model_version")) != version
                    or body.get("model_alias") != "champion"
                ):
                    raise ValueError(
                        "Prediction validity or concrete model version check failed"
                    )
            if sorted(latencies)[18] > float(
                os.getenv("POST_PROMOTION_P95_SECONDS", "0.25")
            ):
                raise ValueError("Prediction p95 latency exceeds promotion guardrail")
            return
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OverflowError,
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Serving version, prediction or latency validation failed"
                ) from None
            time.sleep(5)


def deploy_approved_version(
    client: MlflowClient, name: str, requested: str = "", *, operation_id: str = ""
) -> str:
    """Select an approved model, verify serving and restore the previous model on failure."""
    if requested and (not requested.isdigit() or int(requested) < 1):
        raise ValueError("Select a positive registry version or the current champion")
    current = _champion(client, name, "champion")
    version = requested or current
    identity = (
        operation_id
        or hashlib.sha256(f"{name}:{version}:{time.time_ns()}".encode()).hexdigest()
    )
    promotion = begin_promotion(
        client,
        name,
        version,
        operation_id=identity,
        approved_by="protected-ML-Deploy",
        approved_at=str(time.time()),
        require_approved=True,
    )
    if promotion["status"] == "rolled_back":
        raise RuntimeError(
            "This deployment already rolled back; inspect before starting another operation"
        )
    try:
        verify_serving_version(version)
        complete_promotion(client, promotion, serving_verified=True)
    except Exception:
        if promotion["status"] != "completed":
            rollback_promotion_verified(
                client,
                promotion,
                reason="manual deployment serving validation failed",
                verifier=verify_serving_version,
            )
        raise
    return version
