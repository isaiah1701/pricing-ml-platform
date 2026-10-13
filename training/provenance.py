"""Build reproducible code snapshots and hashes for training provenance."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TypedDict

LOGGER = logging.getLogger(__name__)

SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {".html", ".json", ".lock", ".py", ".tf", ".toml", ".yaml", ".yml"}
)
SOURCE_FILENAMES: frozenset[str] = frozenset({"Dockerfile", ".dockerignore"})
EXCLUDED_PATH_PARTS: frozenset[str] = frozenset(
    {".git", ".pytest_cache", ".terraform", ".venv", "__pycache__", "mlruns"}
)
HASH_CHUNK_SIZE = 1024 * 1024


class CodeProvenance(TypedDict):
    """Identity and archive path for the exact source tree used by a run."""

    git_commit: str
    git_dirty: bool
    git_state_available: bool
    code_sha256: str
    archive_path: str
    file_count: int


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for one provenance file."""
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        while chunk := source_file.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(project_root: Path) -> list[Path]:
    """Return tracked and non-ignored source files relevant to reproducibility."""
    try:
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
    except OSError:
        completed = None
    if completed is not None and completed.returncode == 0:
        relative_paths = [
            Path(raw_path.decode("utf-8"))
            for raw_path in completed.stdout.split(b"\0")
            if raw_path
        ]
    else:
        LOGGER.warning(
            "Git metadata is unavailable; enumerating deployed source files under %s",
            project_root,
        )
        relative_paths = [
            path.relative_to(project_root)
            for path in project_root.rglob("*")
            if path.is_file()
            and not EXCLUDED_PATH_PARTS.intersection(path.relative_to(project_root).parts)
        ]
    selected = [
        relative_path
        for relative_path in relative_paths
        if relative_path.suffix in SOURCE_SUFFIXES
        or relative_path.name in SOURCE_FILENAMES
    ]
    existing = [path for path in selected if (project_root / path).is_file()]
    if not existing:
        LOGGER.error("No reproducible source files were found under %s", project_root)
        raise ValueError("Code snapshot requires at least one source file")
    return sorted(existing, key=lambda path: path.as_posix())


def _git_state(project_root: Path) -> tuple[str, bool, bool]:
    """Return the commit, dirty flag, and Git-metadata availability."""
    try:
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return os.getenv("GIT_COMMIT", "unknown"), False, False
    if commit_result.returncode != 0:
        return os.getenv("GIT_COMMIT", "unknown"), False, False
    commit = commit_result.stdout.strip()
    status_result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status_result.returncode != 0:
        LOGGER.error("Could not inspect Git worktree state under %s", project_root)
        raise RuntimeError("Git worktree inspection failed")
    return commit, bool(status_result.stdout.strip()), True


def build_code_snapshot(
    project_root: str | Path,
    output_directory: str | Path,
) -> CodeProvenance:
    """Archive and hash the exact non-ignored source files used for training."""
    resolved_root = Path(project_root).expanduser().resolve()
    resolved_output = Path(output_directory).expanduser().resolve()
    files = _source_files(resolved_root)
    git_commit, git_dirty, git_state_available = _git_state(resolved_root)
    tree_digest = hashlib.sha256()
    for relative_path in files:
        file_digest = _sha256_file(resolved_root / relative_path)
        tree_digest.update(relative_path.as_posix().encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(file_digest.encode("ascii"))
        tree_digest.update(b"\n")
    code_sha256 = tree_digest.hexdigest()

    resolved_output.mkdir(parents=True, exist_ok=True)
    archive_path = resolved_output / f"source-{code_sha256}.tar.gz"
    if not archive_path.exists():
        with tempfile.NamedTemporaryFile(
            dir=resolved_output, prefix=".source-", delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        try:
            with (
                temporary_path.open("wb") as raw_archive,
                gzip.GzipFile(fileobj=raw_archive, mode="wb", mtime=0) as compressed,
                tarfile.open(fileobj=compressed, mode="w") as archive,
            ):
                for relative_path in files:
                    source_path = resolved_root / relative_path
                    archive_info = archive.gettarinfo(
                        str(source_path), arcname=relative_path.as_posix()
                    )
                    archive_info.uid = 0
                    archive_info.gid = 0
                    archive_info.uname = ""
                    archive_info.gname = ""
                    archive_info.mtime = 0
                    with source_path.open("rb") as source_file:
                        archive.addfile(archive_info, source_file)
            temporary_path.replace(archive_path)
            archive_path.chmod(0o444)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    LOGGER.info(
        "Captured code snapshot %s from %d files (dirty=%s)",
        code_sha256,
        len(files),
        git_dirty,
    )
    return {
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "git_state_available": git_state_available,
        "code_sha256": code_sha256,
        "archive_path": str(archive_path),
        "file_count": len(files),
    }


def sha256_directory(directory: str | Path) -> str:
    """Return a stable digest of all files below a model artifact directory."""
    resolved_directory = Path(directory).expanduser().resolve()
    if not resolved_directory.is_dir():
        LOGGER.error("Model artifact directory does not exist: %s", resolved_directory)
        raise NotADirectoryError(f"Artifact directory not found: {resolved_directory}")
    digest = hashlib.sha256()
    files = sorted(
        (path for path in resolved_directory.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(resolved_directory).as_posix(),
    )
    if not files:
        raise ValueError("Model artifact directory contains no files")
    for path in files:
        relative_path = path.relative_to(resolved_directory).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
