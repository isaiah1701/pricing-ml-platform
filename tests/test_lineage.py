"""Tests for content-addressed snapshots and dataset lineage manifests."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from botocore.exceptions import ClientError

from training.lineage import (
    find_latest_lineage_manifest,
    load_lineage_manifest,
    publish_dataset_snapshot,
    resolve_git_commit,
    sha256_file,
    verify_dataset_snapshot,
)

LOGGER = logging.getLogger(__name__)


@pytest.fixture
def dataset_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create a raw input and one derived Parquet dataset."""
    raw_path = tmp_path / "raw.csv"
    raw_path.write_text("id,value\n1,10\n", encoding="utf-8")
    dataset_path = tmp_path / "features.parquet"
    pd.DataFrame({"id": [1], "value": [10.0]}).to_parquet(
        dataset_path, index=False
    )
    return raw_path, dataset_path


def test_snapshot_is_content_addressed_and_preserves_old_bytes(
    dataset_files: tuple[Path, Path], tmp_path: Path
) -> None:
    """Retain the original snapshot when the mutable source later changes."""
    raw_path, dataset_path = dataset_files
    snapshot_root = tmp_path / "snapshots"
    first = publish_dataset_snapshot(
        dataset_path,
        snapshot_root,
        source_path=raw_path,
        validation_report={"valid": True, "blocking_failures": []},
        transformation_name="tests.transform",
        git_commit="abc123",
        feature_service_name="test_features_v1",
    )
    first_bytes = Path(first["snapshot_path"]).read_bytes()

    pd.DataFrame({"id": [1], "value": [99.0]}).to_parquet(
        dataset_path, index=False
    )
    second = publish_dataset_snapshot(
        dataset_path,
        snapshot_root,
        source_path=raw_path,
        validation_report={"valid": True, "blocking_failures": []},
        transformation_name="tests.transform",
        git_commit="abc123",
        feature_service_name="test_features_v1",
    )

    assert first["snapshot_id"] != second["snapshot_id"]
    assert Path(first["snapshot_path"]).read_bytes() == first_bytes
    assert Path(first["snapshot_path"]).stat().st_mode & 0o222 == 0
    verify_dataset_snapshot(first["snapshot_path"], first["dataset_sha256"])


def test_manifest_links_source_snapshot_schema_and_transformation(
    dataset_files: tuple[Path, Path], tmp_path: Path
) -> None:
    """Record sufficient provenance to trace a model input to its source."""
    raw_path, dataset_path = dataset_files
    published = publish_dataset_snapshot(
        dataset_path,
        tmp_path / "snapshots",
        source_path=raw_path,
        validation_report={"valid": True, "blocking_failures": []},
        transformation_name="tests.transform",
        git_commit="abc123",
        feature_service_name="test_features_v1",
    )

    manifest = load_lineage_manifest(published["lineage_manifest_path"])
    dataset = manifest["dataset"]
    upstream = manifest["upstreams"][0]
    transformation = manifest["transformation"]
    assert dataset["sha256"] == sha256_file(dataset_path)
    assert dataset["columns"] == ["id", "value"]
    assert upstream["sha256"] == sha256_file(raw_path)
    assert transformation["git_commit"] == "abc123"
    assert manifest["validation"]["valid"] is True
    assert (
        find_latest_lineage_manifest(published["snapshot_path"])
        == Path(published["lineage_manifest_path"])
    )


def test_invalid_dataset_cannot_be_published(
    dataset_files: tuple[Path, Path], tmp_path: Path
) -> None:
    """Refuse immutable publication when blocking data checks failed."""
    raw_path, dataset_path = dataset_files
    with pytest.raises(ValueError, match="validated dataset"):
        publish_dataset_snapshot(
            dataset_path,
            tmp_path / "snapshots",
            source_path=raw_path,
            validation_report={"valid": False, "blocking_failures": ["types"]},
            transformation_name="tests.transform",
            git_commit="abc123",
            feature_service_name="test_features_v1",
        )


def test_tampered_lineage_manifest_fails_integrity_check(
    dataset_files: tuple[Path, Path], tmp_path: Path
) -> None:
    """Reject a manifest whose content no longer matches its lineage ID."""
    raw_path, dataset_path = dataset_files
    published = publish_dataset_snapshot(
        dataset_path,
        tmp_path / "snapshots",
        source_path=raw_path,
        validation_report={"valid": True, "blocking_failures": []},
        transformation_name="tests.transform",
        git_commit="abc123",
        feature_service_name="test_features_v1",
    )
    manifest_path = Path(published["lineage_manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["feature_service"] = "tampered"
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match its identity"):
        load_lineage_manifest(manifest_path)


def test_snapshot_is_published_once_to_content_addressed_s3_keys(
    dataset_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload immutable snapshot objects with checksums and create-only writes."""
    raw_path, dataset_path = dataset_files
    client = MagicMock()
    missing = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )
    dataset_digest = sha256_file(dataset_path)
    client.head_object.side_effect = [
        missing,
        {"Metadata": {"sha256": dataset_digest}},
        missing,
        {"Metadata": {"sha256": "manifest-digest-placeholder"}},
    ]

    original_sha256_file = sha256_file

    def stable_sha256_file(path: str | Path) -> str:
        """Return a fixed manifest digest while preserving dataset hashing."""
        resolved = Path(path)
        if resolved.suffix == ".json":
            return "manifest-digest-placeholder"
        return original_sha256_file(path)

    monkeypatch.setattr("training.lineage.sha256_file", stable_sha256_file)
    published = publish_dataset_snapshot(
        dataset_path,
        tmp_path / "snapshots",
        source_path=raw_path,
        validation_report={"valid": True, "blocking_failures": []},
        transformation_name="tests.transform",
        git_commit="abc123",
        feature_service_name="test_features_v1",
        object_store_uri="s3://immutable-bucket/snapshots",
        s3_client=client,
    )

    assert published["snapshot_object_uri"] == (
        f"s3://immutable-bucket/snapshots/{dataset_digest}/listing_features.parquet"
    )
    assert published["lineage_manifest_object_uri"].startswith(
        f"s3://immutable-bucket/snapshots/{dataset_digest}/lineage/"
    )
    assert client.put_object.call_count == 2
    for call in client.put_object.call_args_list:
        assert call.kwargs["IfNoneMatch"] == "*"
        assert call.kwargs["Metadata"]["sha256"]


def test_git_commit_uses_build_identity_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain the CI commit identity in minimal production containers."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("GIT_COMMIT", "deployed-commit")
    assert resolve_git_commit(tmp_path) == "deployed-commit"
