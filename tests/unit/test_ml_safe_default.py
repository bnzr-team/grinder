"""Tests for M8 ML safe-by-default behavior.

The safe-by-default contract ensures that:
1. ml_enabled=False produces the same digest as baseline
2. ml_enabled=True with no signal.json produces the same digest as baseline
3. Only when ml_enabled=True AND signal.json exists do features change

This is a critical contract for M8 milestone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grinder.ml import PROBS_SUM_BPS, MlSignalSnapshot, MlSignalValidationError
from grinder.paper import PaperEngine

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


class TestMlSignalSnapshot:
    """Tests for MlSignalSnapshot contract."""

    def test_valid_signal(self) -> None:
        """Test creating a valid signal."""
        signal = MlSignalSnapshot(
            ts_ms=1706000000000,
            symbol="BTCUSDT",
            regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
            predicted_regime="MID",
            spacing_multiplier_x1000=1200,
        )
        assert signal.ts_ms == 1706000000000
        assert signal.symbol == "BTCUSDT"
        assert sum(signal.regime_probs_bps.values()) == PROBS_SUM_BPS

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        signal = MlSignalSnapshot(
            ts_ms=1706000000000,
            symbol="ETHUSDT",
            regime_probs_bps={"LOW": 1000, "MID": 3000, "HIGH": 6000},
            predicted_regime="HIGH",
            spacing_multiplier_x1000=1500,
        )
        d = signal.to_dict()
        restored = MlSignalSnapshot.from_dict(d)
        assert restored == signal

    def test_roundtrip_json(self) -> None:
        """Test JSON serialization roundtrip."""
        signal = MlSignalSnapshot(
            ts_ms=1706000000000,
            symbol="BTCUSDT",
            regime_probs_bps={"LOW": 3333, "MID": 3333, "HIGH": 3334},
            predicted_regime="LOW",
            spacing_multiplier_x1000=800,
        )
        json_str = signal.to_json()
        restored = MlSignalSnapshot.from_json(json_str)
        assert restored == signal

    def test_json_deterministic(self) -> None:
        """Test that JSON serialization is deterministic."""
        signal = MlSignalSnapshot(
            ts_ms=1706000000000,
            symbol="BTCUSDT",
            regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
            predicted_regime="MID",
            spacing_multiplier_x1000=1000,
        )
        json1 = signal.to_json()
        json2 = signal.to_json()
        assert json1 == json2

    def test_to_policy_features(self) -> None:
        """Test conversion to policy features (all integers)."""
        signal = MlSignalSnapshot(
            ts_ms=1706000000000,
            symbol="BTCUSDT",
            regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
            predicted_regime="MID",
            spacing_multiplier_x1000=1200,
        )
        features = signal.to_policy_features()

        # All values must be integers
        for key, value in features.items():
            assert isinstance(value, int), f"{key} should be int, got {type(value)}"

        assert features["ml_regime_prob_low_bps"] == 2000
        assert features["ml_regime_prob_mid_bps"] == 5000
        assert features["ml_regime_prob_high_bps"] == 3000
        assert features["ml_spacing_multiplier_x1000"] == 1200
        assert features["ml_predicted_regime_ord"] == 1  # MID = 1

    def test_frozen(self) -> None:
        """Test immutability."""
        signal = MlSignalSnapshot(
            ts_ms=1706000000000,
            symbol="BTCUSDT",
            regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
            predicted_regime="MID",
            spacing_multiplier_x1000=1000,
        )
        with pytest.raises(AttributeError):
            signal.ts_ms = 0  # type: ignore[misc]


class TestMlSignalValidation:
    """Tests for MlSignalSnapshot validation."""

    def test_invalid_regime_keys(self) -> None:
        """Test that invalid regime keys are rejected."""
        with pytest.raises(MlSignalValidationError, match="regime_probs_bps keys"):
            MlSignalSnapshot(
                ts_ms=1706000000000,
                symbol="BTCUSDT",
                regime_probs_bps={"WRONG": 5000, "MID": 5000},  # Wrong keys
                predicted_regime="MID",
                spacing_multiplier_x1000=1000,
            )

    def test_probs_sum_not_10000(self) -> None:
        """Test that probabilities must sum to 10000."""
        with pytest.raises(MlSignalValidationError, match="must sum to"):
            MlSignalSnapshot(
                ts_ms=1706000000000,
                symbol="BTCUSDT",
                regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 2000},  # Sum = 9000
                predicted_regime="MID",
                spacing_multiplier_x1000=1000,
            )

    def test_negative_prob(self) -> None:
        """Test that negative probabilities are rejected."""
        with pytest.raises(MlSignalValidationError, match="must be >= 0"):
            MlSignalSnapshot(
                ts_ms=1706000000000,
                symbol="BTCUSDT",
                regime_probs_bps={"LOW": -1000, "MID": 5000, "HIGH": 6000},
                predicted_regime="MID",
                spacing_multiplier_x1000=1000,
            )

    def test_invalid_predicted_regime(self) -> None:
        """Test that invalid predicted_regime is rejected."""
        with pytest.raises(MlSignalValidationError, match="predicted_regime"):
            MlSignalSnapshot(
                ts_ms=1706000000000,
                symbol="BTCUSDT",
                regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
                predicted_regime="INVALID",
                spacing_multiplier_x1000=1000,
            )

    def test_zero_spacing_multiplier(self) -> None:
        """Test that zero spacing multiplier is rejected."""
        with pytest.raises(MlSignalValidationError, match="spacing_multiplier_x1000 must be > 0"):
            MlSignalSnapshot(
                ts_ms=1706000000000,
                symbol="BTCUSDT",
                regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
                predicted_regime="MID",
                spacing_multiplier_x1000=0,
            )

    def test_negative_ts_ms(self) -> None:
        """Test that negative timestamp is rejected."""
        with pytest.raises(MlSignalValidationError, match="ts_ms must be >= 0"):
            MlSignalSnapshot(
                ts_ms=-1,
                symbol="BTCUSDT",
                regime_probs_bps={"LOW": 2000, "MID": 5000, "HIGH": 3000},
                predicted_regime="MID",
                spacing_multiplier_x1000=1000,
            )


class TestMlSafeByDefault:
    """Tests for M8 safe-by-default contract.

    The safe-by-default contract ensures that:
    - ml_enabled=False produces baseline digest
    - ml_enabled=True with no signal.json produces same baseline digest
    """

    def test_ml_disabled_matches_baseline(self) -> None:
        """Test that ml_enabled=False produces deterministic baseline digest.

        This is the control: running without ML produces a known digest.
        """
        fixture_path = FIXTURES_DIR / "sample_day"
        if not fixture_path.exists():
            pytest.skip("sample_day fixture not found")

        # Run with ml_enabled=False (default)
        engine1 = PaperEngine(ml_enabled=False)
        result1 = engine1.run(fixture_path)

        engine2 = PaperEngine(ml_enabled=False)
        result2 = engine2.run(fixture_path)

        assert result1.digest == result2.digest, "Baseline should be deterministic"

    def test_ml_enabled_no_signal_matches_baseline(self) -> None:
        """Test that ml_enabled=True with no signal.json matches ml_enabled=False.

        This is the critical safe-by-default test:
        - sample_day fixture has no ml/signal.json
        - ml_enabled=True should NOT change the digest
        """
        fixture_path = FIXTURES_DIR / "sample_day"
        if not fixture_path.exists():
            pytest.skip("sample_day fixture not found")

        # Verify there's no ML signal file
        signal_path = fixture_path / "ml" / "signal.json"
        assert not signal_path.exists(), "sample_day should not have ml/signal.json"

        # Run with ml_enabled=False (baseline)
        engine_off = PaperEngine(ml_enabled=False)
        result_off = engine_off.run(fixture_path)

        # Run with ml_enabled=True (no signal.json exists)
        engine_on = PaperEngine(ml_enabled=True)
        result_on = engine_on.run(fixture_path)

        # Safe-by-default: digests MUST match
        assert result_off.digest == result_on.digest, (
            f"SAFE-BY-DEFAULT VIOLATION: "
            f"ml_enabled=False digest {result_off.digest} != "
            f"ml_enabled=True digest {result_on.digest}"
        )
