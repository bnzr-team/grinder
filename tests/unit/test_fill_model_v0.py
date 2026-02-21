"""Tests for grinder.ml.fill_model_v0 (Track C, PR-C2).

Covers:
- FillModelV0: predict range, default prior, train, save/load roundtrip.
- Determinism: train twice → byte-identical model.json.
- Manifest integrity: sha256 matches, tampered file raises ValueError.
- No sklearn import: pure-Python baseline.
- Train script: CLI roundtrip via _load_dataset + FillModelV0.train.
- Empty dataset: model returns default prior.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from scripts.train_fill_model_v0 import _load_dataset

import grinder.ml.fill_model_v0 as _fill_model_v0_mod
from grinder.ml.fill_dataset import (
    FillOutcomeRow,
    RoundtripTracker,
    build_fill_dataset_v1,
)
from grinder.ml.fill_model_v0 import (
    FillModelFeaturesV0,
    FillModelV0,
    extract_features,
)
from grinder.paper.fills import Fill

if TYPE_CHECKING:
    from pathlib import Path

# --- Helpers ---------------------------------------------------------------

_D = Decimal


def _make_row(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    entry_ts: int = 1000,
    entry_price: str = "50000",
    entry_qty: str = "0.1",
    entry_fee: str = "0",
    entry_fill_count: int = 1,
    exit_ts: int = 2000,
    exit_price: str = "51000",
    exit_qty: str = "0.1",
    exit_fee: str = "0",
    exit_fill_count: int = 1,
    realized_pnl: str = "100",
    net_pnl: str = "100",
    pnl_bps: int = 200,
    holding_time_ms: int = 1000,
    notional: str = "5000",
    outcome: str = "win",
) -> FillOutcomeRow:
    """Convenience constructor for test FillOutcomeRow objects."""
    return FillOutcomeRow(
        row_id="test_row",
        symbol=symbol,
        direction=direction,
        entry_ts=entry_ts,
        entry_price=_D(entry_price),
        entry_qty=_D(entry_qty),
        entry_fee=_D(entry_fee),
        entry_fill_count=entry_fill_count,
        exit_ts=exit_ts,
        exit_price=_D(exit_price),
        exit_qty=_D(exit_qty),
        exit_fee=_D(exit_fee),
        exit_fill_count=exit_fill_count,
        realized_pnl=_D(realized_pnl),
        net_pnl=_D(net_pnl),
        pnl_bps=pnl_bps,
        holding_time_ms=holding_time_ms,
        notional=_D(notional),
        outcome=outcome,
        source="paper",
        dataset_version="v1",
    )


def _make_training_rows() -> list[FillOutcomeRow]:
    """Build a mixed set of win/loss rows for training."""
    rows = []
    # 6 wins, 4 losses → global prior ~6000 bps.
    for i in range(6):
        rows.append(
            _make_row(
                entry_ts=1000 + i * 100,
                exit_ts=2000 + i * 100,
                holding_time_ms=1000,
                outcome="win",
                notional="5000",
            )
        )
    for i in range(4):
        rows.append(
            _make_row(
                entry_ts=5000 + i * 100,
                exit_ts=6000 + i * 100,
                holding_time_ms=1000,
                outcome="loss",
                net_pnl="-100",
                pnl_bps=-200,
                notional="5000",
            )
        )
    return rows


# --- REQ-001: predict returns int in range -----------------------------------


class TestPredictReturnsIntInRange:
    """REQ-001: predict_fill_prob_bps returns int in 0..10000."""

    def test_trained_model_range(self) -> None:
        rows = _make_training_rows()
        model = FillModelV0.train(rows)
        features = extract_features(rows[0])
        prob = model.predict(features)

        assert isinstance(prob, int)
        assert 0 <= prob <= 10000

    def test_various_features(self) -> None:
        rows = _make_training_rows()
        model = FillModelV0.train(rows)

        test_features: list[FillModelFeaturesV0] = [
            FillModelFeaturesV0(
                direction="long",
                notional_bucket=0,
                entry_fill_count=1,
                holding_ms_bucket=0,
            ),
            FillModelFeaturesV0(
                direction="short",
                notional_bucket=4,
                entry_fill_count=3,
                holding_ms_bucket=4,
            ),
            FillModelFeaturesV0(
                direction="long",
                notional_bucket=2,
                entry_fill_count=2,
                holding_ms_bucket=2,
            ),
        ]

        for feat in test_features:
            prob = model.predict(feat)
            assert isinstance(prob, int)
            assert 0 <= prob <= 10000


# --- REQ-002: train builds bins ----------------------------------------------


class TestTrainBuildsBins:
    """REQ-002: FillModelV0.train builds calibration bins."""

    def test_bins_non_empty_after_training(self) -> None:
        rows = _make_training_rows()
        model = FillModelV0.train(rows)

        assert len(model.bins) > 0
        assert model.n_train_rows == len(rows)
        assert model.global_prior_bps > 0

    def test_win_rate_reflected_in_bins(self) -> None:
        """All-win dataset should produce 10000 bps bins."""
        rows = [_make_row(outcome="win") for _ in range(5)]
        model = FillModelV0.train(rows)

        features = extract_features(rows[0])
        assert model.predict(features) == 10000

    def test_all_loss_dataset(self) -> None:
        """All-loss dataset should produce 0 bps bins."""
        rows = [_make_row(outcome="loss", net_pnl="-100") for _ in range(5)]
        model = FillModelV0.train(rows)

        features = extract_features(rows[0])
        assert model.predict(features) == 0


# --- REQ-003: no sklearn import ----------------------------------------------


class TestNoSklearnImport:
    """REQ-003: fill_model_v0.py has zero sklearn/numpy/scipy imports."""

    def test_no_ml_deps_imported(self) -> None:
        source = pathlib.Path(
            _fill_model_v0_mod.__file__
        ).read_text()
        for dep in ("sklearn", "numpy", "scipy", "pandas"):
            assert f"import {dep}" not in source


# --- REQ-004: manifest integrity ---------------------------------------------


class TestManifestIntegrity:
    """REQ-004: manifest sha256 matches actual model.json."""

    def test_sha256_matches(self, tmp_path: Path) -> None:
        rows = _make_training_rows()
        model = FillModelV0.train(rows)
        model_dir = model.save(
            tmp_path / "model_v0",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        manifest = json.loads((model_dir / "manifest.json").read_text())
        model_bytes = (model_dir / "model.json").read_text()
        actual_sha = hashlib.sha256(model_bytes.encode()).hexdigest()

        assert manifest["sha256"]["model.json"] == actual_sha


# --- REQ-005: determinism digest stable --------------------------------------


class TestDeterminismDigestStable:
    """REQ-005: two trains on same data → identical model.json."""

    def test_two_trains_identical(self, tmp_path: Path) -> None:
        rows = _make_training_rows()

        model1 = FillModelV0.train(rows)
        dir1 = model1.save(
            tmp_path / "run1",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        model2 = FillModelV0.train(rows)
        dir2 = model2.save(
            tmp_path / "run2",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        bytes1 = (dir1 / "model.json").read_text()
        bytes2 = (dir2 / "model.json").read_text()
        assert bytes1 == bytes2

        m1 = json.loads((dir1 / "manifest.json").read_text())
        m2 = json.loads((dir2 / "manifest.json").read_text())
        assert m1 == m2


# --- REQ-006: train script roundtrip -----------------------------------------


class TestTrainScriptRoundtrip:
    """REQ-006: CLI reads dataset, trains, writes artifact."""

    def test_load_dataset_and_train(self, tmp_path: Path) -> None:
        # Build a fill_outcomes_v1 dataset first.
        tracker = RoundtripTracker()
        tracker.record(
            Fill(
                ts=1000,
                symbol="BTCUSDT",
                side="BUY",
                price=_D("50000"),
                quantity=_D("0.1"),
                order_id="o1",
            )
        )
        row = tracker.record(
            Fill(
                ts=2000,
                symbol="BTCUSDT",
                side="SELL",
                price=_D("51000"),
                quantity=_D("0.1"),
                order_id="o2",
            )
        )
        assert row is not None

        dataset_dir = build_fill_dataset_v1(
            rows=[row],
            out_dir=tmp_path / "dataset",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        # Load dataset using script helper.
        loaded_rows = _load_dataset(dataset_dir)
        assert len(loaded_rows) == 1
        assert loaded_rows[0].symbol == "BTCUSDT"

        # Train and save model.
        model = FillModelV0.train(loaded_rows)
        model_dir = model.save(
            tmp_path / "model_v0",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        assert (model_dir / "model.json").exists()
        assert (model_dir / "manifest.json").exists()


# --- REQ-007: save/load roundtrip --------------------------------------------


class TestSaveLoadRoundtrip:
    """REQ-007: train → save → load → predict returns same value."""

    def test_roundtrip_prediction_stable(self, tmp_path: Path) -> None:
        rows = _make_training_rows()
        model = FillModelV0.train(rows)

        features = extract_features(rows[0])
        prob_before = model.predict(features)

        model_dir = model.save(
            tmp_path / "model_v0",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        loaded = FillModelV0.load(model_dir)
        prob_after = loaded.predict(features)

        assert prob_before == prob_after


# --- Tampered model raises ValueError ----------------------------------------


class TestManifestSha256Mismatch:
    """sha256 mismatch on load raises ValueError."""

    def test_tampered_model_raises(self, tmp_path: Path) -> None:
        rows = _make_training_rows()
        model = FillModelV0.train(rows)
        model_dir = model.save(
            tmp_path / "model_v0",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        # Tamper with model.json.
        model_path = model_dir / "model.json"
        model_path.write_text("tampered content\n")

        with pytest.raises(ValueError, match="SHA256 mismatch"):
            FillModelV0.load(model_dir)


# --- Empty dataset -----------------------------------------------------------


class TestEmptyDataset:
    """Empty dataset: model returns default 5000 bps."""

    def test_empty_rows_default_prior(self, tmp_path: Path) -> None:
        model = FillModelV0.train([])
        assert model.global_prior_bps == 5000
        assert model.n_train_rows == 0
        assert len(model.bins) == 0

        features = FillModelFeaturesV0(
            direction="long",
            notional_bucket=2,
            entry_fill_count=1,
            holding_ms_bucket=2,
        )
        assert model.predict(features) == 5000

        # Save/load roundtrip on empty model.
        model_dir = model.save(
            tmp_path / "empty_model",
            created_at_utc="2026-01-01T00:00:00Z",
        )
        loaded = FillModelV0.load(model_dir)
        assert loaded.predict(features) == 5000
