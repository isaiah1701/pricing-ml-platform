"""Publish immutable dataset snapshots and machine-readable lineage manifests."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from urllib.parse import urlparse

import boto3
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)

HASH_CHUNK_SIZE = 1024 * 1024
LINEAGE_SCHEMA_VERSION = 1


class PublishedSnapshot(TypedDict):
    """References identifying one immutable dataset and its lineage record."""

    snapshot_id: str
    dataset_sha256: str
    snapshot_path: str
    lineage_id: str
    lineage_manifest_path: str
    source_sha256: str
    snapshot_object_uri: NotRequired[str]
    lineage_manifest_object_uri: NotRequired[str]


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file without loading it into memory."""
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.is_file():
        LOGGER.error("Cannot hash missing file: %s", resolved_path)
        raise FileNotFoundError(f"File not found: {resolved_path}")
    digest = hashlib.sha256()
    with resolved_path.open("rb") as source_file:
        while chunk := source_file.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_git_commit(project_root: str | Path) -> str:
    """Return the checked-out Git commit SHA, or ``unknown`` when unavailable."""
    resolved_root = Path(project_root).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=resolved_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        LOGGER.warning("Git executable is unavailable under %s", resolved_root)
        return os.getenv("GIT_COMMIT", "unknown")
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not commit:
        LOGGER.warning("Git SHA could not be resolved from %s", resolved_root)
        return os.getenv("GIT_COMMIT", "unknown")
    return commit


def _publish_file_once(source_path: Path, destination_path: Path, digest: str) -> None:
    """Atomically publish a read-only file without replacing an existing object."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        if sha256_file(destination_path) != digest:
            LOGGER.error("Snapshot digest collision at %s", destination_path)
            raise ValueError(
                f"Existing snapshot has unexpected content: {destination_path}"
            )
        return

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination_path.parent,
            prefix=".snapshot-",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with source_path.open("rb") as source_file:
                shutil.copyfileobj(source_file, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if sha256_file(temporary_path) != digest:
            LOGGER.error("Snapshot copy verification failed for %s", source_path)
            raise ValueError("Snapshot copy does not match the source dataset")
        temporary_path.chmod(0o444)
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError:
            if sha256_file(destination_path) != digest:
                raise ValueError(
                    f"Concurrent snapshot publication conflict: {destination_path}"
                )
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_bytes_once(destination_path: Path, content: bytes) -> None:
    """Atomically write immutable bytes without replacing an existing manifest."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        if destination_path.read_bytes() != content:
            LOGGER.error("Lineage manifest collision at %s", destination_path)
            raise ValueError(f"Existing lineage manifest differs: {destination_path}")
        return
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination_path.parent,
            prefix=".lineage-",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(0o444)
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError:
            if destination_path.read_bytes() != content:
                LOGGER.error("Lineage manifest collision at %s", destination_path)
                raise ValueError(
                    f"Existing lineage manifest differs: {destination_path}"
                )
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Return the bucket and normalized key prefix from an S3 URI."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        LOGGER.error("Immutable snapshot destination is not a valid S3 URI")
        raise ValueError("Snapshot object-store URI must use s3://bucket/prefix")
    return parsed.netloc, parsed.path.strip("/")


def _upload_immutable_object(
    path: Path,
    *,
    bucket: str,
    key: str,
    digest: str,
    s3_client: Any,
) -> str:
    """Create one checksum-tagged S3 object without replacing existing bytes."""
    try:
        existing = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        if error_code not in {"404", "NoSuchKey", "NotFound"}:
            LOGGER.error("Could not inspect immutable S3 object s3://%s/%s", bucket, key)
            raise
    else:
        metadata = existing.get("Metadata", {})
        if metadata.get("sha256") != digest:
            LOGGER.error("Immutable S3 object checksum conflict at s3://%s/%s", bucket, key)
            raise ValueError(f"Existing immutable object has the wrong checksum: s3://{bucket}/{key}")
        return f"s3://{bucket}/{key}"

    try:
        with path.open("rb") as source_file:
            s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=source_file,
                IfNoneMatch="*",
                Metadata={"sha256": digest},
            )
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        if error_code not in {"412", "PreconditionFailed"}:
            LOGGER.error("Could not publish immutable S3 object s3://%s/%s", bucket, key)
            raise

    uploaded = s3_client.head_object(Bucket=bucket, Key=key)
    if uploaded.get("Metadata", {}).get("sha256") != digest:
        LOGGER.error("Uploaded S3 checksum verification failed at s3://%s/%s", bucket, key)
        raise ValueError(f"Uploaded immutable object failed verification: s3://{bucket}/{key}")
    LOGGER.info("Published immutable object s3://%s/%s", bucket, key)
    return f"s3://{bucket}/{key}"


def _publish_snapshot_objects(
    published: PublishedSnapshot,
    destination_uri: str,
    s3_client: Any | None,
) -> PublishedSnapshot:
    """Publish a snapshot and lineage manifest under content-derived S3 keys."""
    bucket, prefix = _parse_s3_uri(destination_uri)
    client = s3_client or boto3.client("s3")
    snapshot_id = published["snapshot_id"]
    snapshot_key = "/".join(
        part for part in (prefix, snapshot_id, "listing_features.parquet") if part
    )
    manifest_name = Path(published["lineage_manifest_path"]).name
    manifest_key = "/".join(
        part for part in (prefix, snapshot_id, "lineage", manifest_name) if part
    )
    snapshot_uri = _upload_immutable_object(
        Path(published["snapshot_path"]),
        bucket=bucket,
        key=snapshot_key,
        digest=published["dataset_sha256"],
        s3_client=client,
    )
    manifest_path = Path(published["lineage_manifest_path"])
    manifest_uri = _upload_immutable_object(
        manifest_path,
        bucket=bucket,
        key=manifest_key,
        digest=sha256_file(manifest_path),
        s3_client=client,
    )
    return {
        **published,
        "snapshot_object_uri": snapshot_uri,
        "lineage_manifest_object_uri": manifest_uri,
    }


def publish_dataset_snapshot(
    dataset_path: str | Path,
    snapshot_root: str | Path,
    *,
    source_path: str | Path,
    validation_report: Mapping[str, Any],
    transformation_name: str,
    git_commit: str,
    feature_service_name: str,
    source_role: str = "raw_input",
    object_store_uri: str | None = None,
    s3_client: Any | None = None,
) -> PublishedSnapshot:
    """Publish a content-addressed Parquet snapshot and lineage manifest."""
    resolved_dataset = Path(dataset_path).expanduser().resolve()
    resolved_source = Path(source_path).expanduser().resolve()
    resolved_root = Path(snapshot_root).expanduser().resolve()
    if not resolved_dataset.is_file():
        LOGGER.error("Dataset snapshot input does not exist: %s", resolved_dataset)
        raise FileNotFoundError(f"Dataset not found: {resolved_dataset}")
    if not resolved_source.is_file():
        LOGGER.error("Lineage source does not exist: %s", resolved_source)
        raise FileNotFoundError(f"Lineage source not found: {resolved_source}")
    if not bool(validation_report.get("valid")):
        LOGGER.error("Refusing to snapshot a dataset without passing validation")
        raise ValueError("Only a validated dataset can be snapshotted")

    dataset_sha256 = sha256_file(resolved_dataset)
    source_sha256 = sha256_file(resolved_source)
    snapshot_directory = resolved_root / dataset_sha256
    snapshot_path = snapshot_directory / "listing_features.parquet"
    _publish_file_once(resolved_dataset, snapshot_path, dataset_sha256)

    parquet_file = pq.ParquetFile(snapshot_path)
    parquet_schema = parquet_file.schema_arrow
    schema_sha256 = hashlib.sha256(str(parquet_schema).encode("utf-8")).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    manifest_body: dict[str, object] = {
        "lineage_schema_version": LINEAGE_SCHEMA_VERSION,
        "created_at_utc": created_at,
        "dataset": {
            "snapshot_id": dataset_sha256,
            "path": str(snapshot_path),
            "sha256": dataset_sha256,
            "format": "parquet",
            "size_bytes": snapshot_path.stat().st_size,
            "row_count": parquet_file.metadata.num_rows,
            "columns": parquet_schema.names,
            "schema_sha256": schema_sha256,
        },
        "upstreams": [
            {
                "role": source_role,
                "path": str(resolved_source),
                "sha256": source_sha256,
                "size_bytes": resolved_source.stat().st_size,
            }
        ],
        "transformation": {
            "name": transformation_name,
            "git_commit": git_commit,
        },
        "validation": dict(validation_report),
        "feature_service": feature_service_name,
    }
    canonical_body = json.dumps(
        manifest_body, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    lineage_id = hashlib.sha256(canonical_body).hexdigest()
    manifest = {"lineage_id": lineage_id, **manifest_body}
    manifest_path = snapshot_directory / "lineage" / f"{lineage_id}.json"
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
    ).encode("utf-8")
    if sha256_file(resolved_source) != source_sha256:
        LOGGER.error(
            "Upstream source changed during lineage capture: %s", resolved_source
        )
        raise ValueError("Upstream source changed during lineage capture")
    _write_bytes_once(manifest_path, manifest_bytes)
    LOGGER.info(
        "Published immutable dataset snapshot %s with lineage %s",
        dataset_sha256,
        lineage_id,
    )
    published: PublishedSnapshot = {
        "snapshot_id": dataset_sha256,
        "dataset_sha256": dataset_sha256,
        "snapshot_path": str(snapshot_path),
        "lineage_id": lineage_id,
        "lineage_manifest_path": str(manifest_path),
        "source_sha256": source_sha256,
    }
    if object_store_uri:
        published = _publish_snapshot_objects(
            published,
            object_store_uri,
            s3_client,
        )
    return published


def verify_dataset_snapshot(snapshot_path: str | Path, expected_sha256: str) -> None:
    """Raise when a snapshot no longer matches its content-derived identity."""
    actual_sha256 = sha256_file(snapshot_path)
    if actual_sha256 != expected_sha256:
        LOGGER.error(
            "Dataset snapshot verification failed: expected=%s actual=%s",
            expected_sha256,
            actual_sha256,
        )
        raise ValueError("Dataset snapshot content no longer matches its identity")


def load_lineage_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Load and cryptographically validate a lineage manifest JSON object."""
    resolved_path = Path(manifest_path).expanduser().resolve()
    if not resolved_path.is_file():
        LOGGER.error("Lineage manifest does not exist: %s", resolved_path)
        raise FileNotFoundError(f"Lineage manifest not found: {resolved_path}")
    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        LOGGER.error("Lineage manifest is not a JSON object: %s", resolved_path)
        raise TypeError("Lineage manifest must be a JSON object")
    required = {"lineage_id", "dataset", "upstreams", "transformation", "validation"}
    missing = sorted(required.difference(payload))
    if missing:
        LOGGER.error("Lineage manifest is missing fields: %s", missing)
        raise ValueError(f"Lineage manifest is missing fields: {missing}")
    recorded_lineage_id = str(payload["lineage_id"])
    manifest_body = {
        key: value for key, value in payload.items() if key != "lineage_id"
    }
    canonical_body = json.dumps(
        manifest_body, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    actual_lineage_id = hashlib.sha256(canonical_body).hexdigest()
    if actual_lineage_id != recorded_lineage_id:
        LOGGER.error("Lineage manifest integrity check failed: %s", resolved_path)
        raise ValueError("Lineage manifest content does not match its identity")
    return payload


def find_latest_lineage_manifest(snapshot_path: str | Path) -> Path | None:
    """Return the newest valid manifest adjacent to a content snapshot, if any."""
    resolved_snapshot = Path(snapshot_path).expanduser().resolve()
    lineage_directory = resolved_snapshot.parent / "lineage"
    if not lineage_directory.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    snapshot_sha256 = sha256_file(resolved_snapshot)
    for manifest_path in lineage_directory.glob("*.json"):
        payload = load_lineage_manifest(manifest_path)
        dataset = payload.get("dataset")
        if isinstance(dataset, dict) and dataset.get("sha256") == snapshot_sha256:
            candidates.append((str(payload.get("created_at_utc", "")), manifest_path))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]
