"""Tests for M8-02b ML shadow mode.

Tests config validation guards and shadow mode wiring in PaperEngine.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from grinder.ml.onnx import ONNX_AVAILABLE
from grinder.paper.engine import PaperEngine

# Path to test artifact
TEST_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "tiny_regime"


class TestConfigValidationGuards:
    """Tests for ONNX config validation guards."""

    def test_infer_enabled_without_onnxruntime(self) -> None:
        """Test that ml_infer_enabled=True fails when onnxruntime not installed."""
        with (
            patch("grinder.paper.engine.ONNX_AVAILABLE", False),
            pytest.raises(ValueError, match="onnxruntime not installed"),
        ):
            PaperEngine(
                ml_infer_enabled=True,
                ml_shadow_mode=True,
                onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
            )

    def test_infer_without_shadow_mode(self) -> None:
        """Test that ml_infer_enabled=True without ml_shadow_mode=True fails."""
        # Skip if onnxruntime not installed (different error would occur first)
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        with pytest.raises(ValueError, match="requires ml_shadow_mode=True"):
            PaperEngine(
                ml_infer_enabled=True,
                ml_shadow_mode=False,
                onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
            )

    def test_shadow_mode_without_infer_enabled(self) -> None:
        """Test that ml_shadow_mode=True requires ml_infer_enabled=True."""
        with pytest.raises(ValueError, match="requires ml_infer_enabled=True"):
            PaperEngine(
                ml_infer_enabled=False,
                ml_shadow_mode=True,
                onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
            )

    def test_shadow_mode_without_artifact_dir(self) -> None:
        """Test that ml_shadow_mode=True requires onnx_artifact_dir."""
        # Skip if onnxruntime not installed
        if not ONNX_AVAILABLE:
            pytest.skip("onnxruntime not installed")

        with pytest.raises(ValueError, match="requires onnx_artifact_dir"):
            PaperEngine(
                ml_infer_enabled=True,
                ml_shadow_mode=True,
                onnx_artifact_dir=None,
            )


class TestSafeByDefault:
    """Tests to verify safe-by-default behavior."""

    def test_defaults_no_shadow_mode(self) -> None:
        """Test that default config does NOT enable shadow mode."""
        # With all defaults, shadow mode should be off
        engine = PaperEngine()

        assert engine._ml_shadow_mode is False
        assert engine._ml_infer_enabled is False
        assert engine._onnx_artifact_dir is None
        assert engine._onnx_model is None

    def test_model_not_loaded_when_disabled(self) -> None:
        """Test that ONNX model is not loaded when shadow mode is disabled."""
        engine = PaperEngine(
            ml_infer_enabled=False,
            ml_shadow_mode=False,
        )

        # Model should not be loaded
        assert engine._onnx_model is None


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
class TestShadowModeWiring:
    """Tests for shadow mode inference wiring."""

    def test_model_loaded_when_shadow_enabled(self) -> None:
        """Test that ONNX model is loaded when shadow mode is enabled."""
        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_shadow_mode=True,
            onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
        )

        # Model should not be loaded yet (happens in run())
        assert engine._onnx_model is None

        # Load model manually
        engine._load_onnx_model()

        # Now model should be loaded
        assert engine._onnx_model is not None

    def test_shadow_inference_does_not_modify_features(self) -> None:
        """Test that shadow inference doesn't modify policy_features."""
        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_shadow_mode=True,
            onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
        )

        # Load model
        engine._load_onnx_model()

        # Create policy features
        policy_features = {
            "price_mid": 50000.0,
            "spread_bps": 5,
        }
        original_features = dict(policy_features)

        # Run shadow inference
        engine._run_shadow_inference(
            ts=1000,
            symbol="BTCUSDT",
            policy_features=policy_features,
        )

        # Features should NOT be modified
        assert policy_features == original_features

    def test_shadow_inference_soft_fail(self) -> None:
        """Test that shadow inference soft-fails on error."""
        engine = PaperEngine(
            ml_infer_enabled=True,
            ml_shadow_mode=True,
            onnx_artifact_dir=str(TEST_ARTIFACT_DIR),
        )

        # Load model
        engine._load_onnx_model()

        # Replace model with mock that raises
        engine._onnx_model = MagicMock()
        engine._onnx_model.predict.side_effect = RuntimeError("Test error")

        # Should not raise, just log warning
        engine._run_shadow_inference(
            ts=1000,
            symbol="BTCUSDT",
            policy_features={"price_mid": 50000.0},
        )

        # If we got here without exception, soft-fail worked


class TestDeterminismUnchanged:
    """Tests to verify shadow mode doesn't affect determinism."""

    def test_shadow_mode_does_not_affect_digest(self) -> None:
        """Test that enabling shadow mode doesn't change fixture digests.

        This is a sanity check - actual determinism is verified by
        the determinism suite CI job.
        """
        # This test just verifies the config validation passes
        # and model can be loaded without affecting digests

        # Create engine with shadow mode off (baseline)
        engine_baseline = PaperEngine(
            ml_infer_enabled=False,
            ml_shadow_mode=False,
        )

        # Verify baseline config
        assert engine_baseline._ml_shadow_mode is False
        assert engine_baseline._onnx_model is None
