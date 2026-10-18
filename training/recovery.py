"""Persist approved models independently of MLflow's disposable registry database."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
import mlflow
from botocore.exceptions import ClientError
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from training.lineage import sha256_file
from training.provenance import sha256_directory
from training.registry import tag_version_before_alias

LOGGER = logging.getLogger(__name__)
REQUIRED_TAGS = {
    "release_bundle_id",
    "training_attempt_id",
    "mlflow_run_id",
    "code_sha256",
    "dataset_sha256",
    "model_artifact_sha256",
}


def _validate_identity(store: str, name: str, alias: str) -> None:
    """Keep object keys and registry filters within one explicit model identity."""
    if not store.strip() or not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", item) and item not in {".", ".."}
        for item in (name, alias)
    ):
        LOGGER.error("Recovery requires a store and simple model/alias identifiers")
        raise ValueError("Invalid recovery store or model identity")


def _canonical(value: Any) -> bytes:
    """Serialize manifest identities deterministically and reject non-finite numbers."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _read(store: str, key: str) -> bytes:
    """Read one recovery object from S3 or a local test store."""
    parsed = urlparse(store)
    if parsed.scheme == "s3":
        try:
            return (
                boto3.client("s3")
                .get_object(
                    Bucket=parsed.netloc, Key=f"{parsed.path.strip('/')}/{key}"
                )["Body"]
                .read()
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                raise FileNotFoundError(key) from exc
            raise
    if parsed.scheme:
        raise ValueError("Recovery store must be an s3:// URI or local directory")
    return (Path(store) / key).read_bytes()


def _write(store: str, key: str, data: bytes) -> None:
    """Write complete objects atomically and avoid duplicate immutable versions."""
    try:
        if _read(store, key) == data:
            return
    except FileNotFoundError:
        pass
    parsed = urlparse(store)
    if parsed.scheme == "s3":
        boto3.client("s3").put_object(
            Bucket=parsed.netloc,
            Key=f"{parsed.path.strip('/')}/{key}",
            Body=data,
            ServerSideEncryption="AES256",
        )
    else:
        path = Path(store) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)


def _alias(client: MlflowClient, name: str, alias: str) -> Any | None:
    """Distinguish a missing alias from an unavailable or unauthorized registry."""
    try:
        model = client.get_registered_model(name)
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_DOES_NOT_EXIST":
            raise
        return None
    version = model.aliases.get(alias)
    return client.get_model_version(name, version) if version else None


def _verify_provenance(
    tags: dict[str, str], bundle: dict[str, Any], run_id: str
) -> None:
    """Require an internally consistent approved release identity before recovery."""
    body = {key: value for key, value in bundle.items() if key != "release_bundle_id"}
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    valid = REQUIRED_TAGS.issubset(tags) and all(tags.get(key) for key in REQUIRED_TAGS)
    valid = valid and digest == bundle.get("release_bundle_id") == tags.get(
        "release_bundle_id"
    )
    valid = valid and tags.get("mlflow_run_id") == run_id
    for key in REQUIRED_TAGS - {"release_bundle_id"}:
        valid = valid and str(bundle.get(key, "")) == tags.get(key)
    for key in (
        "release_bundle_id",
        "code_sha256",
        "dataset_sha256",
        "model_artifact_sha256",
    ):
        valid = valid and bool(re.fullmatch(r"[0-9a-f]{64}", tags.get(key, "")))
    if not valid:
        LOGGER.error("Approved model has inconsistent recovery provenance")
        raise ValueError("Approved model provenance failed integrity verification")


def export_approved(
    client: MlflowClient, store: str, name: str, alias: str = "champion"
) -> str:
    """Save model bytes and registry provenance, publishing the pointer last."""
    _validate_identity(store, name, alias)
    mlflow.set_tracking_uri(client.tracking_uri)
    version = _alias(client, name, alias)
    if version is None or version.status != "READY":
        raise ValueError("A READY approved alias is required for persistence")
    LOGGER.info("Persisting approved %s version %s", name, version.version)
    run = client.get_run(version.run_id)
    if run.data.tags.get("artifact_reload_verified") != "true":
        raise ValueError(
            "Approved source run must have passed artifact reload verification"
        )
    with tempfile.TemporaryDirectory(prefix="model-export-") as temporary:
        directory = Path(temporary)
        bundle_path = client.download_artifacts(
            version.run_id, "provenance/release_bundle.json", str(directory)
        )
        bundle = json.loads(Path(bundle_path).read_text())
        _verify_provenance(version.tags, bundle, version.run_id)
        downloaded = Path(
            mlflow.artifacts.download_artifacts(
                # Use the same logged-model URI that training hashed. Downloading
                # via models:/name/version changes MLflow's generated metadata.
                artifact_uri=bundle["model_uri"],
                tracking_uri=client.tracking_uri,
                dst_path=str(directory / "download"),
            )
        )
        files: dict[str, str] = {}
        for path in sorted(downloaded.rglob("*")):
            if path.is_symlink():
                raise ValueError("Recovery does not support symlinked model files")
            if path.is_file():
                files[path.relative_to(downloaded).as_posix()] = sha256_file(path)
        if (
            "MLmodel" not in files
            or sha256_directory(downloaded) != version.tags["model_artifact_sha256"]
        ):
            raise ValueError("Approved model artifacts failed integrity verification")
        manifest = {
            "schema_version": 1,
            "name": name,
            "alias": alias,
            "original_version": str(version.version),
            "original_run_id": version.run_id,
            "original_source": version.source,
            "tags": version.tags,
            "run_params": run.data.params,
            "run_metrics": run.data.metrics,
            "release_bundle": bundle,
            "files": files,
        }
        encoded = _canonical(manifest)
        identity = hashlib.sha256(encoded).hexdigest()
        prefix = f"bundles/{identity}"
        for relative in files:
            _write(
                store,
                f"{prefix}/model/{relative}",
                (downloaded / relative).read_bytes(),
            )
        _write(store, f"{prefix}/manifest.json", encoded)
        # Content identity binds the alias pointer to metadata and every artifact.
        _write(
            store,
            f"aliases/{name}/{alias}.json",
            _canonical({"bundle_sha256": identity}),
        )
    LOGGER.info("Approved model recovery bundle persisted: %s", identity)
    return identity


def restore_approved(
    client: MlflowClient, store: str, name: str, alias: str = "champion"
) -> str:
    """Restore a missing alias from verified bytes without training or replacing an existing alias."""
    _validate_identity(store, name, alias)
    existing = _alias(client, name, alias)
    if existing is not None:
        LOGGER.info(
            "Approved alias already exists at version %s; recovery skipped",
            existing.version,
        )
        return str(existing.version)
    pointer = json.loads(_read(store, f"aliases/{name}/{alias}.json"))
    identity = pointer["bundle_sha256"]
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("Invalid recovery pointer")
    prefix = f"bundles/{identity}"
    encoded = _read(store, f"{prefix}/manifest.json")
    if hashlib.sha256(encoded).hexdigest() != identity:
        raise ValueError("Recovery manifest checksum mismatch")
    manifest = json.loads(encoded)
    if (
        manifest["schema_version"] != 1
        or manifest["name"] != name
        or manifest["alias"] != alias
    ):
        raise ValueError("Recovery manifest identifies a different approved model")
    _verify_provenance(
        manifest["tags"], manifest["release_bundle"], manifest["original_run_id"]
    )
    LOGGER.info("Restoring approved model from recovery bundle %s", identity)
    with tempfile.TemporaryDirectory(prefix="model-restore-") as temporary:
        model_path = Path(temporary) / "model"
        for relative, digest in manifest["files"].items():
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in relative
                or not path.parts
            ):
                raise ValueError("Unsafe recovery artifact path")
            data = _read(store, f"{prefix}/model/{relative}")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Recovery artifact checksum mismatch")
            target = model_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if (
            not (model_path / "MLmodel").is_file()
            or sha256_directory(model_path) != manifest["tags"]["model_artifact_sha256"]
        ):
            raise ValueError("Recovery model directory checksum mismatch")
        # No registry mutation occurs until all bytes and provenance pass checks.
        matches = [
            version
            for version in client.search_model_versions(f"name = '{name}'")
            if version.tags.get("recovery_bundle_id") == identity
        ]
        if matches:
            version = matches[0]
            run = client.get_run(version.run_id)
        else:
            experiment = client.get_experiment_by_name("approved-model-recovery")
            experiment_id = (
                experiment.experiment_id
                if experiment
                else client.create_experiment("approved-model-recovery")
            )
            run = client.create_run(
                experiment_id,
                tags={
                    "original_run_id": manifest["original_run_id"],
                    "original_version": manifest["original_version"],
                    "recovery_bundle_id": identity,
                    "artifact_reload_verified": "true",
                },
            )
            client.log_artifacts(run.info.run_id, str(model_path), "model")
            try:
                client.create_registered_model(name)
            except MlflowException as exc:
                if exc.error_code != "RESOURCE_ALREADY_EXISTS":
                    raise
            version = client.create_model_version(
                name,
                f"{run.info.artifact_uri}/model",
                run_id=run.info.run_id,
                tags={"recovery_bundle_id": identity},
            )
        run_id = run.info.run_id
        source = f"{run.info.artifact_uri}/model"
        body = {
            key: value
            for key, value in manifest["release_bundle"].items()
            if key != "release_bundle_id"
        }
        body.update(mlflow_run_id=run_id, model_uri=source)
        bundle_id = hashlib.sha256(_canonical(body)).hexdigest()
        client.log_dict(
            run_id,
            {"release_bundle_id": bundle_id, **body},
            "provenance/release_bundle.json",
        )
        client.log_dict(run_id, manifest, "recovery/original-manifest.json")
        for key, value in manifest["run_params"].items():
            client.log_param(run_id, key, value)
        for key, value in manifest["run_metrics"].items():
            client.log_metric(run_id, key, value)
        tags = {
            **manifest["tags"],
            "mlflow_run_id": run_id,
            "release_bundle_id": bundle_id,
            "original_run_id": manifest["original_run_id"],
            "original_version": manifest["original_version"],
            "recovery_bundle_id": identity,
        }
        client.set_terminated(run_id)
        if version.status != "READY":
            raise ValueError("Restored model version is not READY")
        # Callers serialize recovery/promotion. Never overwrite a concurrently created alias.
        concurrent = _alias(client, name, alias)
        if concurrent is not None:
            raise RuntimeError(
                "Alias appeared during recovery; inspect registry before retrying"
            )
        tag_version_before_alias(client, name, str(version.version), alias, tags)
    LOGGER.info("Approved alias restored as version %s", version.version)
    return str(version.version)


def persist_if_configured(client: MlflowClient, name: str, alias: str) -> None:
    """Back up champion changes when a retained store is configured."""
    store = os.getenv("APPROVED_MODEL_STORE", "").strip()
    if store and alias == "champion":
        export_approved(client, store, name, alias)


def main() -> None:
    """Export or restore the approved alias using the configured MLflow and S3 identities."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "restore"])
    parser.add_argument(
        "--store",
        default=os.getenv("APPROVED_MODEL_STORE"),
        required=not os.getenv("APPROVED_MODEL_STORE"),
    )
    parser.add_argument("--name", default="airbnb-price-predictor")
    parser.add_argument("--alias", default="champion")
    args = parser.parse_args()
    client = MlflowClient()
    operation = export_approved if args.operation == "export" else restore_approved
    operation(client, args.store, args.name, args.alias)


if __name__ == "__main__":
    main()
