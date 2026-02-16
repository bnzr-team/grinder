"""Integration test: Train → Export → Load → Inference roundtrip.

M8-03b: Verifies the complete ML training pipeline produces
artifacts that work with the inference infrastructure.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, ClassVar

import pytest

from grinder.ml.onnx import ONNX_AVAILABLE

# Conditional imports - guarded by ONNX_AVAILABLE
if ONNX_AVAILABLE:
    from scripts.train_regime_model import train_and_export

    from grinder.ml.onnx import OnnxMlModel, load_artifact

pytestmark = pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")


class TestTrainToInferenceRoundtrip:
    """End-to-end test: train model, export artifact, load, inference."""

    def test_full_roundtrip(self) -> None:
        """Test complete pipeline: train → export → load → predict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "test_artifact"

            # Step 1: Train and export
            report = train_and_export(
                out_dir=artifact_dir,
                dataset_id="roundtrip_test",
                seed=42,
                n_samples=100,
            )

            # Verify training succeeded
            assert report.train_accuracy > 0.5
            assert artifact_dir.exists()

            # Step 2: Validate artifact
            artifact = load_artifact(artifact_dir)
            assert artifact.manifest.schema_version == "v1.1"
            assert artifact.model_path.exists()

            # Step 3: Load model for inference
            model = OnnxMlModel.load_from_dir(artifact_dir)
            assert model is not None

            # Step 4: Run inference
            result = model.predict(
                ts_ms=1000000,
                symbol="BTCUSDT",
                policy_features={
                    "price_mid": 50000.0,
                    "price_bid": 49990.0,
                    "price_ask": 50010.0,
                    "spread_bps": 20,
                    "volume_24h": 1e8,
                    "volatility_1h_bps": 150,
                },
            )

            # Verify inference output
            assert result is not None
            assert result.ts_ms == 1000000
            assert result.symbol == "BTCUSDT"
            assert result.predicted_regime in ("LOW", "MID", "HIGH")
            assert sum(result.regime_probs_bps.values()) == 10000
            assert result.spacing_multiplier_x1000 >= 1

    def test_determinism_across_train_load_cycles(self) -> None:
        """Test that same seed produces identical predictions after reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Train two artifacts with same parameters
            art1 = Path(tmpdir) / "art1"
            art2 = Path(tmpdir) / "art2"

            train_and_export(art1, "det_test", seed=123, n_samples=100)
            train_and_export(art2, "det_test", seed=123, n_samples=100)

            # Load both models
            model1 = OnnxMlModel.load_from_dir(art1)
            model2 = OnnxMlModel.load_from_dir(art2)

            # Same features
            features = {
                "price_mid": 60000.0,
                "spread_bps": 10,
                "volatility_1h_bps": 100,
            }

            # Run predictions
            r1 = model1.predict(ts_ms=1000, symbol="ETHUSDT", policy_features=features)
            r2 = model2.predict(ts_ms=1000, symbol="ETHUSDT", policy_features=features)

            # Results must be identical
            assert r1 is not None
            assert r2 is not None
            assert r1.predicted_regime == r2.predicted_regime
            assert r1.regime_probs_bps == r2.regime_probs_bps
            assert r1.spacing_multiplier_x1000 == r2.spacing_multiplier_x1000

    def test_inference_soft_fail_on_corrupt_model(self) -> None:
        """Test that inference soft-fails (returns None) with corrupt model."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            train_and_export(artifact_dir, "test", seed=42, n_samples=50)

            model = OnnxMlModel.load_from_dir(artifact_dir)

            # Replace session with broken one
            class BrokenSession:
                input_names: ClassVar[list[str]] = ["input"]
                output_names: ClassVar[list[str]] = ["regime_probs"]

                def run(self, _inputs: dict[str, Any]) -> dict[str, Any]:
                    raise RuntimeError("Simulated failure")

            model._session = BrokenSession()  # type: ignore[assignment]

            # Should return None, not raise
            result = model.predict(
                ts_ms=1000,
                symbol="TEST",
                policy_features={"price_mid": 1000},
            )

            assert result is None

    def test_different_inputs_different_outputs(self) -> None:
        """Test that model produces different outputs for different inputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            train_and_export(artifact_dir, "test", seed=42, n_samples=200)

            model = OnnxMlModel.load_from_dir(artifact_dir)

            # Low volatility features (should predict LOW or MID)
            low_features = {
                "price_mid": 50000.0,
                "spread_bps": 5,
                "volatility_1h_bps": 50,
            }

            # High volatility features (should predict HIGH or MID)
            high_features = {
                "price_mid": 50000.0,
                "spread_bps": 40,
                "volatility_1h_bps": 400,
            }

            r_low = model.predict(ts_ms=1000, symbol="BTC", policy_features=low_features)
            r_high = model.predict(ts_ms=1000, symbol="BTC", policy_features=high_features)

            assert r_low is not None
            assert r_high is not None

            # Probabilities should differ (model learned something)
            assert r_low.regime_probs_bps != r_high.regime_probs_bps

    def test_model_stats_tracking(self) -> None:
        """Test that model tracks prediction statistics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            train_and_export(artifact_dir, "test", seed=42, n_samples=50)

            model = OnnxMlModel.load_from_dir(artifact_dir)

            initial_stats = model.stats
            assert initial_stats["predict_count"] == 0

            # Make some predictions
            for i in range(5):
                model.predict(
                    ts_ms=i * 1000,
                    symbol="TEST",
                    policy_features={"price_mid": 50000.0},
                )

            final_stats = model.stats
            assert final_stats["predict_count"] == 5
