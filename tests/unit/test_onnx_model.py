"""Tests for M8-02b ONNX model inference.

Tests OnnxMlModel loading and prediction with the tiny test artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from grinder.ml.onnx import (
    ONNX_AVAILABLE,
    OnnxMlModel,
    OnnxRuntimeError,
    vectorize,
)

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
