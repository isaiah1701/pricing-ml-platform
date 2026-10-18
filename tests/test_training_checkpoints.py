"""Verify native CatBoost resume, completed-member reuse and checkpoint isolation."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from catboost import CatBoostRegressor

from training.checkpoints import checkpoint_directory
from training.train import CatBoostPriceEnsembleRegressor

LOGGER = logging.getLogger(__name__)


class IterationObserver:
    """Observe CatBoost's resume position and optionally interrupt after a snapshot."""

    def __init__(self, snapshot: Path, interrupt: bool) -> None:
        """Store the snapshot to wait for and capture the first reported iteration."""
        self.snapshot = snapshot
        self.interrupt = interrupt
        self.first_iteration: int | None = None

    def after_iteration(self, info: Any) -> bool:
        """Stop the test fit only after a real resumable snapshot has been written."""
        if self.first_iteration is None:
            self.first_iteration = info.iteration
        if self.interrupt:
            if self.snapshot.exists() and info.iteration >= 20:
                return False
            time.sleep(0.015)
        return True


def test_interrupted_member_resumes_native_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume actual unfinished CatBoost iterations and match uninterrupted predictions."""
    features = pd.DataFrame({"latitude": np.linspace(40, 41, 64)})
    target = np.linspace(30, 150, 64)
    original_fit = CatBoostRegressor.fit
    observers: list[IterationObserver] = []

    def observed_fit(
        self: CatBoostRegressor, *args: Any, **kwargs: Any
    ) -> CatBoostRegressor:
        """Inject a test callback while retaining the real CatBoost snapshot implementation."""
        observer = IterationObserver(
            Path(kwargs["snapshot_file"]), interrupt=not observers
        )
        observers.append(observer)
        return original_fit(self, *args, **kwargs, callbacks=[observer])

    monkeypatch.setattr(CatBoostRegressor, "fit", observed_fit)
    parameters = {
        "feature_names": ("latitude",),
        "random_states": (42,),
        "iterations": (150,),
        "depth": 3,
        "checkpoint_dir": str(tmp_path),
        "snapshot_interval": 1,
    }
    with pytest.raises(RuntimeError, match="stopped before"):
        CatBoostPriceEnsembleRegressor(**parameters).fit(features, target)
    assert list(tmp_path.rglob("training.cbsnapshot"))
    assert not list(tmp_path.rglob("complete.json"))
    resumed = CatBoostPriceEnsembleRegressor(**parameters).fit(features, target)
    assert observers[1].first_iteration is not None and observers[1].first_iteration > 1
    monkeypatch.setattr(CatBoostRegressor, "fit", original_fit)
    uninterrupted = CatBoostPriceEnsembleRegressor(
        **{**parameters, "checkpoint_dir": ""}
    ).fit(features, target)
    np.testing.assert_allclose(
        resumed.predict(features), uninterrupted.predict(features), rtol=1e-10
    )
    assert list(tmp_path.rglob("complete.json"))


def test_completed_members_are_reused_and_corruption_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip completed members entirely and refuse a damaged completed model."""
    features = pd.DataFrame({"latitude": [40.0, 40.1, 40.2, 40.3]})
    target = np.array([40.0, 60.0, 80.0, 100.0])
    parameters = {
        "feature_names": ("latitude",),
        "random_states": (42, 7),
        "iterations": (5, 5),
        "depth": 2,
        "checkpoint_dir": str(tmp_path),
    }
    first = CatBoostPriceEnsembleRegressor(**parameters).fit(features, target)
    blocked_fit = Mock(
        side_effect=AssertionError("completed models should not train again")
    )
    monkeypatch.setattr(CatBoostRegressor, "fit", blocked_fit)
    repeated = CatBoostPriceEnsembleRegressor(**parameters).fit(features, target)
    blocked_fit.assert_not_called()
    np.testing.assert_allclose(first.predict(features), repeated.predict(features))
    next(tmp_path.rglob("model.cbm")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        CatBoostPriceEnsembleRegressor(**parameters).fit(features, target)


def test_changed_inputs_and_parameters_use_new_checkpoints(tmp_path: Path) -> None:
    """Never reuse a model when ordered data or a training parameter changes."""
    features = pd.DataFrame({"latitude": [40.0, 40.1, 40.2, 40.3]})
    target = np.array([40.0, 60.0, 80.0, 100.0])
    parameters = {
        "feature_names": ("latitude",),
        "random_states": (42,),
        "iterations": (5,),
        "depth": 2,
        "checkpoint_dir": str(tmp_path),
    }
    CatBoostPriceEnsembleRegressor(**parameters).fit(features, target)
    CatBoostPriceEnsembleRegressor(**parameters).fit(features, target + 1)
    CatBoostPriceEnsembleRegressor(**{**parameters, "depth": 3}).fit(features, target)
    assert len(list(tmp_path.glob("*/member-0/complete.json"))) == 3


def test_concurrent_checkpoint_writers_are_rejected(tmp_path: Path) -> None:
    """Protect a shared PVC checkpoint from overlapping training processes."""
    with checkpoint_directory(tmp_path, "same-inputs"), pytest.raises(
        RuntimeError, match="owns this checkpoint"
    ), checkpoint_directory(tmp_path, "same-inputs"):
        pytest.fail("second writer acquired the lock")
