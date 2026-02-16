"""Tests for M8-02b ONNX model inference.

Tests OnnxMlModel loading and prediction with the tiny test artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from grinder.ml.onnx import (
    ONNX_AVAILABLE,
    OnnxMlModel,
    OnnxRuntimeError,
    vectorize,
)
from grinder.ml.onnx.features import FEATURE_ORDER

# Path to test artifact
TEST_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "tiny_regime"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
class TestOnnxMlModel:
    """Tests for OnnxMlModel class."""

    def test_load_from_dir(self) -> None:
        """Test loading model from artifact directory."""
        model = OnnxMlModel.load_from_dir(TEST_ARTIFACT_DIR)
        assert model is not None
        assert model.artifact is not None
        assert model.artifact.manifest.schema_version == "v1"

    def test_predict_returns_snapshot(self) -> None:
        """Test that predict returns valid MlSignalSnapshot."""
        model = OnnxMlModel.load_from_dir(TEST_ARTIFACT_DIR)

        # Minimal features
        policy_features = {
            "price_mid": 50000.0,
            "spread_bps": 5,
        }

        result = model.predict(
            ts_ms=1000,
            symbol="BTCUSDT",
            policy_features=policy_features,
        )

        assert result is not None
        assert result.ts_ms == 1000
        assert result.symbol == "BTCUSDT"
        assert result.predicted_regime in ("LOW", "MID", "HIGH")
        assert sum(result.regime_probs_bps.values()) == 10000
        assert result.spacing_multiplier_x1000 >= 1

    def test_predict_soft_fail(self) -> None:
        """Test that predict returns None on error (soft-fail)."""
        model = OnnxMlModel.load_from_dir(TEST_ARTIFACT_DIR)

        # Inject a broken session by replacing with mock
        original_session = model._session

        class BrokenSession:
            @property
            def input_names(self) -> list[str]:
                return ["input"]

            @property
            def output_names(self) -> list[str]:
                return ["regime_probs", "spacing_multiplier"]

            def run(self, _inputs: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("Simulated error")

        model._session = BrokenSession()  # type: ignore[assignment]

        result = model.predict(
            ts_ms=1000,
            symbol="BTCUSDT",
            policy_features={"price_mid": 50000.0},
        )

        # Should return None, not raise
        assert result is None

        # Restore original
        model._session = original_session

    def test_stats_tracking(self) -> None:
        """Test that prediction stats are tracked."""
        model = OnnxMlModel.load_from_dir(TEST_ARTIFACT_DIR)

        initial_stats = model.stats
        assert initial_stats["predict_count"] == 0
        assert initial_stats["predict_errors"] == 0

        # Make a prediction
        model.predict(
            ts_ms=1000,
            symbol="BTCUSDT",
            policy_features={"price_mid": 50000.0},
        )

        stats = model.stats
        assert stats["predict_count"] == 1


class TestVectorize:
    """Tests for feature vectorization."""

    def test_vectorize_basic(self) -> None:
        """Test basic vectorization."""
        features = {
            "price_mid": 50000.0,
            "spread_bps": 5,
        }
        result = vectorize(features)
        assert result.shape[0] == 15  # FEATURE_ORDER has 15 features
        assert result.dtype == np.float32
        assert result[0] == 50000.0  # price_mid is first

    def test_vectorize_missing_features(self) -> None:
        """Test that missing features are filled with 0.0."""
        features: dict[str, float] = {}
        result = vectorize(features)
        assert result.shape[0] == 15
        assert np.all(result == 0.0)

    def test_vectorize_deterministic(self) -> None:
        """Test that vectorization is deterministic."""
        features = {
            "price_mid": 50000.0,
            "spread_bps": 5,
            "volume_24h": 1000000,
        }
        result1 = vectorize(features)
        result2 = vectorize(features)
        assert np.array_equal(result1, result2)


class TestOnnxAvailability:
    """Tests for ONNX availability detection."""

    def test_onnx_available_constant(self) -> None:
        """Test that ONNX_AVAILABLE is a boolean."""
        assert isinstance(ONNX_AVAILABLE, bool)

    @pytest.mark.skipif(ONNX_AVAILABLE, reason="Test only when onnxruntime not installed")
    def test_load_without_onnxruntime(self) -> None:
        """Test that loading fails gracefully without onnxruntime."""
        with pytest.raises(OnnxRuntimeError, match="onnxruntime not installed"):
            OnnxMlModel.load_from_dir(TEST_ARTIFACT_DIR)


# =============================================================================
# M8-03b-2: Runtime Integration Tests
# =============================================================================

# Path to golden artifact (trained with known seed for deterministic output)
GOLDEN_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "golden_regime"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
class TestGoldenArtifactRuntimeDeterminism:
    """M8-03b-2: Tests for bit-for-bit runtime determinism using golden artifact."""

    # Fixed feature vector for determinism tests (all 15 features)
    FULL_FEATURE_VECTOR: ClassVar[dict[str, float]] = {
        "price_mid": 50000.0,
        "price_bid": 49990.0,
        "price_ask": 50010.0,
        "spread_bps": 20,
        "volume_24h": 1e8,
        "volume_1h": 1e7,
        "volatility_1h_bps": 150,
        "volatility_24h_bps": 200,
        "position_size": 0.5,
        "position_notional": 25000.0,
        "position_pnl_bps": 50,
        "grid_levels_active": 10,
        "grid_utilization_pct": 60,
        "trend_strength": 0.3,
        "momentum_1h": 25,
    }

    def test_golden_load_twice_predict_identical(self) -> None:
        """Test that loading golden artifact twice produces bit-for-bit identical predictions.

        This validates that the OnnxMlModel runtime is fully deterministic:
        same model file + same input features = exact same output.
        """
        # Load model twice (separate instances)
        model1 = OnnxMlModel.load_from_dir(GOLDEN_ARTIFACT_DIR)
        model2 = OnnxMlModel.load_from_dir(GOLDEN_ARTIFACT_DIR)

        # Predict with identical inputs
        r1 = model1.predict(
            ts_ms=1000,
            symbol="BTCUSDT",
            policy_features=self.FULL_FEATURE_VECTOR,
        )
        r2 = model2.predict(
            ts_ms=1000,
            symbol="BTCUSDT",
            policy_features=self.FULL_FEATURE_VECTOR,
        )

        # Results must be bit-for-bit identical
        assert r1 is not None
        assert r2 is not None
        assert r1.predicted_regime == r2.predicted_regime
        assert r1.regime_probs_bps == r2.regime_probs_bps
        assert r1.spacing_multiplier_x1000 == r2.spacing_multiplier_x1000

    def test_golden_multiple_predictions_stable(self) -> None:
        """Test that multiple predictions on same model are stable."""
        model = OnnxMlModel.load_from_dir(GOLDEN_ARTIFACT_DIR)

        results = []
        for i in range(5):
            result = model.predict(
                ts_ms=1000 + i,  # Different timestamp, same features
                symbol="BTCUSDT",
                policy_features=self.FULL_FEATURE_VECTOR,
            )
            assert result is not None
            results.append(result)

        # All predictions should have identical probs (timestamp doesn't affect model)
        first = results[0]
        for r in results[1:]:
            assert r.regime_probs_bps == first.regime_probs_bps
            assert r.predicted_regime == first.predicted_regime
            assert r.spacing_multiplier_x1000 == first.spacing_multiplier_x1000

    def test_golden_predict_with_full_feature_vector(self) -> None:
        """Test prediction with all 15 FEATURE_ORDER features populated."""
        model = OnnxMlModel.load_from_dir(GOLDEN_ARTIFACT_DIR)

        # Verify we're testing with all features
        assert len(self.FULL_FEATURE_VECTOR) == len(FEATURE_ORDER)
        assert set(self.FULL_FEATURE_VECTOR.keys()) == set(FEATURE_ORDER)

        result = model.predict(
            ts_ms=1000,
            symbol="ETHUSDT",
            policy_features=self.FULL_FEATURE_VECTOR,
        )

        assert result is not None
        assert result.predicted_regime in ("LOW", "MID", "HIGH")
        assert sum(result.regime_probs_bps.values()) == 10000
        assert result.spacing_multiplier_x1000 >= 1


class TestVectorizeOrderContract:
    """M8-03b-2: Tests for vectorize SSOT contract with FEATURE_ORDER."""

    def test_vectorize_order_matches_feature_order_exactly(self) -> None:
        """Test that vectorize places features at exact FEATURE_ORDER positions."""
        # Create dict with unique values for each feature
        features = {name: float(i + 1) * 100 for i, name in enumerate(FEATURE_ORDER)}

        result = vectorize(features)

        # Verify each position matches
        for i, name in enumerate(FEATURE_ORDER):
            expected = float(i + 1) * 100
            actual = result[i]
            assert actual == expected, f"Position {i} ({name}): expected {expected}, got {actual}"

    def test_vectorize_preserves_feature_order_tuple(self) -> None:
        """Test that FEATURE_ORDER is immutable tuple with 15 elements."""
        assert isinstance(FEATURE_ORDER, tuple)
        assert len(FEATURE_ORDER) == 15
        # First element should be price_mid (SSOT)
        assert FEATURE_ORDER[0] == "price_mid"
        # Last element should be momentum_1h (SSOT)
        assert FEATURE_ORDER[-1] == "momentum_1h"

    def test_vectorize_partial_features_zeros_in_correct_positions(self) -> None:
        """Test that missing features get 0.0 at their FEATURE_ORDER positions."""
        # Only provide first and last feature
        features = {
            "price_mid": 50000.0,
            "momentum_1h": 25.0,
        }

        result = vectorize(features)

        # price_mid at index 0
        assert result[0] == 50000.0
        # momentum_1h at last index
        assert result[len(FEATURE_ORDER) - 1] == 25.0
        # All others should be 0.0
        for i in range(1, len(FEATURE_ORDER) - 1):
            assert result[i] == 0.0, f"Index {i} should be 0.0"

    def test_vectorize_dtype_is_float32(self) -> None:
        """Test that vectorize output is float32 for ONNX compatibility."""
        features = {"price_mid": 50000.0}
        result = vectorize(features)
        assert result.dtype == np.float32
