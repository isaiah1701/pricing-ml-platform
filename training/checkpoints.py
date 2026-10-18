"""Resume CatBoost ensemble training from identity-bound persistent checkpoints."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

LOGGER = logging.getLogger(__name__)


def training_identity(
    features: pd.DataFrame, target: np.ndarray, parameters: Mapping[str, Any]
) -> str:
    """Bind checkpoints to ordered data, model settings, code and dependency versions."""
    digest = hashlib.sha256()
    digest.update(pd.util.hash_pandas_object(features, index=True).to_numpy().tobytes())
    digest.update(target.tobytes())
    description = {
        "format": 1,
        "columns": list(features.columns),
        "dtypes": [str(dtype) for dtype in features.dtypes],
        "parameters": dict(parameters),
        "versions": {
            package: version(package) for package in ("catboost", "pandas", "numpy")
        },
        "source": {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in ("train.py", "checkpoints.py")
        },
    }
    digest.update(json.dumps(description, sort_keys=True, allow_nan=False).encode())
    return digest.hexdigest()


@contextmanager
def checkpoint_directory(root: Path, identity: str) -> Iterator[Path]:
    """Serialize writers to matching training inputs without blocking indefinitely."""
    directory = root.expanduser().resolve() / identity
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            LOGGER.error("Matching training checkpoint is already in use")
            raise RuntimeError("Another training process owns this checkpoint") from exc
        try:
            LOGGER.info("Using training checkpoint %s", identity)
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def fit_member(
    features: pd.DataFrame,
    target: np.ndarray,
    categorical: list[str],
    parameters: dict[str, Any],
    directory: Path,
    member: int,
    interval: int,
) -> CatBoostRegressor:
    """Reuse a completed member or resume its native CatBoost training snapshot."""
    member_dir = directory / f"member-{member}"
    member_dir.mkdir(exist_ok=True)
    completed = member_dir / "model.cbm"
    receipt = member_dir / "complete.json"
    model = CatBoostRegressor(
        **parameters,
        allow_writing_files=True,
        train_dir=str(member_dir / "logs"),
        verbose=False,
    )
    if receipt.is_file():
        metadata = json.loads(receipt.read_text())
        if (
            not completed.is_file()
            or hashlib.sha256(completed.read_bytes()).hexdigest() != metadata["sha256"]
        ):
            LOGGER.error("Completed training member %d failed its checksum", member)
            raise ValueError(
                "Training checkpoint checksum mismatch; inspect before removing it"
            )
        model.load_model(str(completed))
        if model.tree_count_ != parameters["iterations"]:
            raise ValueError("Completed checkpoint has the wrong tree count")
        LOGGER.info("Reusing completed ensemble member %d", member)
        return model
    snapshot = member_dir / "training.cbsnapshot"
    LOGGER.info(
        "%s ensemble member %d", "Resuming" if snapshot.exists() else "Training", member
    )
    try:
        model.fit(
            features,
            target,
            cat_features=categorical,
            verbose=False,
            save_snapshot=True,
            snapshot_file=str(snapshot),
            snapshot_interval=interval,
        )
    except Exception:
        LOGGER.warning(
            "Training member %d interrupted; retaining available checkpoints", member
        )
        raise
    if model.tree_count_ != parameters["iterations"]:
        LOGGER.warning(
            "Training member %d stopped early; retaining its snapshot", member
        )
        raise RuntimeError("Training stopped before all iterations completed")
    temporary = member_dir / "model.partial.cbm"
    model.save_model(str(temporary))
    os.replace(temporary, completed)
    metadata = {
        "sha256": hashlib.sha256(completed.read_bytes()).hexdigest(),
        "iterations": model.tree_count_,
    }
    temporary_receipt = member_dir / "complete.partial.json"
    temporary_receipt.write_text(json.dumps(metadata))
    os.replace(temporary_receipt, receipt)
    LOGGER.info("Saved completed ensemble member %d", member)
    return model
