"""Tests for M8-01b ML signal time-indexed selection.

SSOT selection rule:
- For each (symbol, snapshot.ts_ms), select max(signal.ts_ms) where signal.ts_ms <= snapshot.ts_ms
- If no such signal exists, return None (safe-by-default)

These tests verify:
1. Correct signal selection based on timestamp
2. Safe-by-default when no signal available
3. Duplicate ts_ms validation
4. Multi-symbol independence
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from grinder.ml import MlSignalSnapshot
from grinder.paper import PaperEngine


class TestMlSignalSelection:
    """Tests for _get_ml_signal() time-indexed lookup."""

    def test_selects_exact_timestamp_match(self) -> None:
        """Test that exact ts_ms match is selected."""
        engine = PaperEngine(ml_enabled=True)
        engine._ml_signals = {
            "BTCUSDT": [
                MlSignalSnapshot(
                    ts_ms=1000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 5000, "MID": 3000, "HIGH": 2000},
                    predicted_regime="LOW",
                    spacing_multiplier_x1000=800,
                ),
                MlSignalSnapshot(
                    ts_ms=2000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
                    predicted_regime="MID",
                    spacing_multiplier_x1000=1000,
                ),
            ]
        }

        # Exact match at ts_ms=2000
        signal = engine._get_ml_signal("BTCUSDT", 2000)
        assert signal is not None
        assert signal.ts_ms == 2000
        assert signal.predicted_regime == "MID"

    def test_selects_most_recent_before_timestamp(self) -> None:
        """Test that most recent signal <= ts_ms is selected."""
        engine = PaperEngine(ml_enabled=True)
        engine._ml_signals = {
            "BTCUSDT": [
                MlSignalSnapshot(
                    ts_ms=1000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 5000, "MID": 3000, "HIGH": 2000},
                    predicted_regime="LOW",
                    spacing_multiplier_x1000=800,
                ),
                MlSignalSnapshot(
                    ts_ms=3000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 2000, "MID": 3000, "HIGH": 5000},
                    predicted_regime="HIGH",
                    spacing_multiplier_x1000=1200,
                ),
            ]
        }

        # Query at ts_ms=2500: should get signal from ts_ms=1000
        signal = engine._get_ml_signal("BTCUSDT", 2500)
        assert signal is not None
        assert signal.ts_ms == 1000
        assert signal.predicted_regime == "LOW"

        # Query at ts_ms=5000: should get signal from ts_ms=3000
        signal = engine._get_ml_signal("BTCUSDT", 5000)
        assert signal is not None
        assert signal.ts_ms == 3000
        assert signal.predicted_regime == "HIGH"

    def test_returns_none_when_all_signals_in_future(self) -> None:
        """Test safe-by-default: no signal if all are in the future."""
        engine = PaperEngine(ml_enabled=True)
        engine._ml_signals = {
            "BTCUSDT": [
                MlSignalSnapshot(
                    ts_ms=5000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 3333, "MID": 3333, "HIGH": 3334},
                    predicted_regime="MID",
                    spacing_multiplier_x1000=1000,
                ),
            ]
        }

        # Query at ts_ms=1000: all signals are in future
        signal = engine._get_ml_signal("BTCUSDT", 1000)
        assert signal is None

    def test_returns_none_for_unknown_symbol(self) -> None:
        """Test safe-by-default: no signal for unknown symbol."""
        engine = PaperEngine(ml_enabled=True)
        engine._ml_signals = {
            "BTCUSDT": [
                MlSignalSnapshot(
                    ts_ms=1000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 3333, "MID": 3333, "HIGH": 3334},
                    predicted_regime="MID",
                    spacing_multiplier_x1000=1000,
                ),
            ]
        }

        signal = engine._get_ml_signal("ETHUSDT", 2000)
        assert signal is None

    def test_returns_none_for_empty_signal_list(self) -> None:
        """Test safe-by-default: no signal for empty list."""
        engine = PaperEngine(ml_enabled=True)
        engine._ml_signals = {"BTCUSDT": []}

        signal = engine._get_ml_signal("BTCUSDT", 2000)
        assert signal is None

    def test_multi_symbol_independence(self) -> None:
        """Test that signals are independent per symbol."""
        engine = PaperEngine(ml_enabled=True)
        engine._ml_signals = {
            "BTCUSDT": [
                MlSignalSnapshot(
                    ts_ms=1000,
                    symbol="BTCUSDT",
                    regime_probs_bps={"LOW": 8000, "MID": 1000, "HIGH": 1000},
                    predicted_regime="LOW",
                    spacing_multiplier_x1000=700,
                ),
            ],
            "ETHUSDT": [
                MlSignalSnapshot(
                    ts_ms=2000,
                    symbol="ETHUSDT",
                    regime_probs_bps={"LOW": 1000, "MID": 1000, "HIGH": 8000},
                    predicted_regime="HIGH",
                    spacing_multiplier_x1000=1500,
                ),
            ],
        }

        # BTCUSDT at ts_ms=1500: gets signal from ts_ms=1000
        btc_signal = engine._get_ml_signal("BTCUSDT", 1500)
        assert btc_signal is not None
        assert btc_signal.predicted_regime == "LOW"

        # ETHUSDT at ts_ms=1500: no signal (all in future)
        eth_signal = engine._get_ml_signal("ETHUSDT", 1500)
        assert eth_signal is None

        # ETHUSDT at ts_ms=2500: gets signal from ts_ms=2000
        eth_signal = engine._get_ml_signal("ETHUSDT", 2500)
        assert eth_signal is not None
        assert eth_signal.predicted_regime == "HIGH"


class TestMlSignalLoading:
    """Tests for _load_ml_signals() with validation."""

    def test_loads_and_sorts_signals(self) -> None:
        """Test that signals are loaded and sorted by ts_ms."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            ml_dir = fixture_path / "ml"
            ml_dir.mkdir()

            # Write signals in reverse order (should be sorted)
            signals = [
                {
                    "ts_ms": 3000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 2000, "MID": 5000, "HIGH": 3000},
                    "predicted_regime": "MID",
                    "spacing_multiplier_x1000": 1000,
                },
                {
                    "ts_ms": 1000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 5000, "MID": 3000, "HIGH": 2000},
                    "predicted_regime": "LOW",
                    "spacing_multiplier_x1000": 800,
                },
                {
                    "ts_ms": 2000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 3000, "MID": 3000, "HIGH": 4000},
                    "predicted_regime": "HIGH",
                    "spacing_multiplier_x1000": 1200,
                },
            ]
            (ml_dir / "signal.json").write_text(json.dumps(signals))

            engine = PaperEngine(ml_enabled=True)
            engine._load_ml_signals(fixture_path)

            # Verify sorted by ts_ms
            assert "BTCUSDT" in engine._ml_signals
            loaded = engine._ml_signals["BTCUSDT"]
            assert len(loaded) == 3
            assert loaded[0].ts_ms == 1000
            assert loaded[1].ts_ms == 2000
            assert loaded[2].ts_ms == 3000

    def test_rejects_duplicate_ts_ms(self) -> None:
        """Test that duplicate ts_ms for same symbol raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            ml_dir = fixture_path / "ml"
            ml_dir.mkdir()

            # Two signals with same ts_ms
            signals = [
                {
                    "ts_ms": 1000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 5000, "MID": 3000, "HIGH": 2000},
                    "predicted_regime": "LOW",
                    "spacing_multiplier_x1000": 800,
                },
                {
                    "ts_ms": 1000,  # Duplicate!
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 2000, "MID": 5000, "HIGH": 3000},
                    "predicted_regime": "MID",
                    "spacing_multiplier_x1000": 1000,
                },
            ]
            (ml_dir / "signal.json").write_text(json.dumps(signals))

            engine = PaperEngine(ml_enabled=True)
            with pytest.raises(ValueError, match="Duplicate ts_ms"):
                engine._load_ml_signals(fixture_path)

    def test_allows_same_ts_ms_different_symbols(self) -> None:
        """Test that same ts_ms for different symbols is OK."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            ml_dir = fixture_path / "ml"
            ml_dir.mkdir()

            # Same ts_ms but different symbols
            signals = [
                {
                    "ts_ms": 1000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 5000, "MID": 3000, "HIGH": 2000},
                    "predicted_regime": "LOW",
                    "spacing_multiplier_x1000": 800,
                },
                {
                    "ts_ms": 1000,
                    "symbol": "ETHUSDT",  # Different symbol
                    "regime_probs_bps": {"LOW": 2000, "MID": 5000, "HIGH": 3000},
                    "predicted_regime": "MID",
                    "spacing_multiplier_x1000": 1000,
                },
            ]
            (ml_dir / "signal.json").write_text(json.dumps(signals))

            engine = PaperEngine(ml_enabled=True)
            engine._load_ml_signals(fixture_path)

            assert "BTCUSDT" in engine._ml_signals
            assert "ETHUSDT" in engine._ml_signals
            assert len(engine._ml_signals["BTCUSDT"]) == 1
            assert len(engine._ml_signals["ETHUSDT"]) == 1

    def test_skips_invalid_signals(self) -> None:
        """Test that invalid signals are skipped (safe-by-default)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            ml_dir = fixture_path / "ml"
            ml_dir.mkdir()

            signals = [
                {
                    "ts_ms": 1000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 5000, "MID": 3000, "HIGH": 2000},
                    "predicted_regime": "LOW",
                    "spacing_multiplier_x1000": 800,
                },
                {
                    # Invalid: wrong keys
                    "ts_ms": 2000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"WRONG": 10000},
                    "predicted_regime": "MID",
                    "spacing_multiplier_x1000": 1000,
                },
                {
                    "ts_ms": 3000,
                    "symbol": "BTCUSDT",
                    "regime_probs_bps": {"LOW": 2000, "MID": 5000, "HIGH": 3000},
                    "predicted_regime": "MID",
                    "spacing_multiplier_x1000": 1000,
                },
            ]
            (ml_dir / "signal.json").write_text(json.dumps(signals))

            engine = PaperEngine(ml_enabled=True)
            engine._load_ml_signals(fixture_path)

            # Only valid signals loaded (ts_ms=1000 and ts_ms=3000)
            assert len(engine._ml_signals["BTCUSDT"]) == 2
            assert engine._ml_signals["BTCUSDT"][0].ts_ms == 1000
            assert engine._ml_signals["BTCUSDT"][1].ts_ms == 3000

    def test_no_file_safe_by_default(self) -> None:
        """Test that missing signal.json is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            # No ml/ directory

            engine = PaperEngine(ml_enabled=True)
            engine._load_ml_signals(fixture_path)

            # No signals loaded, no error
            assert len(engine._ml_signals) == 0

    def test_single_signal_format(self) -> None:
        """Test that single signal (not array) format works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_path = Path(tmpdir)
            ml_dir = fixture_path / "ml"
            ml_dir.mkdir()

            # Single object, not array
            signal = {
                "ts_ms": 1000,
                "symbol": "BTCUSDT",
                "regime_probs_bps": {"LOW": 5000, "MID": 3000, "HIGH": 2000},
                "predicted_regime": "LOW",
                "spacing_multiplier_x1000": 800,
            }
            (ml_dir / "signal.json").write_text(json.dumps(signal))

            engine = PaperEngine(ml_enabled=True)
            engine._load_ml_signals(fixture_path)

            assert len(engine._ml_signals["BTCUSDT"]) == 1
            assert engine._ml_signals["BTCUSDT"][0].ts_ms == 1000
