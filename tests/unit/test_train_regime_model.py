"""Tests for M8-03b training pipeline.

Tests the train_regime_model.py script functionality:
- Data generation determinism
- Model training and accuracy
- ONNX export and artifact creation
- Manifest validation
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

# Import training module functions
from scripts.train_regime_model import (
    compute_sha256,
    generate_synthetic_data,
    train_and_export,
    train_model,
)

from grinder.ml.onnx import ONNX_AVAILABLE, load_artifact
from grinder.ml.onnx.features import FEATURE_ORDER


class TestGenerateSyntheticData:
    """Tests for synthetic data generation."""

    def test_generates_correct_shape(self) -> None:
        """Test that data has correct shape."""
        n_samples = 50
        X, y_regime, y_spacing = generate_synthetic_data(n_samples, 42, "test")

        assert X.shape == (n_samples, len(FEATURE_ORDER))
        assert y_regime.shape == (n_samples,)
        assert y_spacing.shape == (n_samples,)

    def test_deterministic_with_same_seed(self) -> None:
        """Test that same seed produces identical data."""
        X1, y1, s1 = generate_synthetic_data(100, 42, "det_test")
        X2, y2, s2 = generate_synthetic_data(100, 42, "det_test")

        assert np.array_equal(X1, X2)
        assert np.array_equal(y1, y2)
        assert np.array_equal(s1, s2)

    def test_different_dataset_id_produces_different_data(self) -> None:
        """Test that different dataset_id produces different data."""
        X1, _, _ = generate_synthetic_data(100, 42, "dataset_a")
        X2, _, _ = generate_synthetic_data(100, 42, "dataset_b")

        assert not np.array_equal(X1, X2)

    def test_different_seed_produces_different_data(self) -> None:
        """Test that different seed produces different data."""
        X1, _, _ = generate_synthetic_data(100, 42, "test")
        X2, _, _ = generate_synthetic_data(100, 43, "test")

        assert not np.array_equal(X1, X2)

    def test_all_classes_represented(self) -> None:
        """Test that all 3 regime classes are represented."""
        _, y_regime, _ = generate_synthetic_data(100, 42, "test")

        unique_classes = set(y_regime)
        assert 0 in unique_classes, "LOW regime (0) missing"
        assert 1 in unique_classes, "MID regime (1) missing"
        assert 2 in unique_classes, "HIGH regime (2) missing"

    def test_regime_labels_valid(self) -> None:
        """Test that regime labels are 0, 1, or 2."""
        _, y_regime, _ = generate_synthetic_data(100, 42, "test")

        assert set(y_regime).issubset({0, 1, 2})

    def test_spacing_multipliers_reasonable(self) -> None:
        """Test that spacing multipliers are in reasonable range."""
        _, _, y_spacing = generate_synthetic_data(100, 42, "test")

        assert np.all(y_spacing >= 0.5)
        assert np.all(y_spacing <= 2.0)

    def test_features_dtype_float32(self) -> None:
        """Test that features are float32 for ONNX compatibility."""
        X, _, _ = generate_synthetic_data(50, 42, "test")

        assert X.dtype == np.float32


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
class TestTrainModel:
    """Tests for model training."""

    def test_train_returns_models(self) -> None:
        """Test that training returns regime and spacing models."""
        X, y_regime, y_spacing = generate_synthetic_data(100, 42, "test")
        regime_model, spacing_model, accuracy = train_model(X, y_regime, y_spacing, 42)

        assert regime_model is not None
        assert spacing_model is not None
        assert 0.0 <= accuracy <= 1.0

    def test_train_accuracy_reasonable(self) -> None:
        """Test that training accuracy is reasonable (not random)."""
        X, y_regime, y_spacing = generate_synthetic_data(100, 42, "test")
        _, _, accuracy = train_model(X, y_regime, y_spacing, 42)

        # Should be better than random (33%)
        assert accuracy > 0.5

    def test_train_deterministic(self) -> None:
        """Test that training is deterministic."""
        X, y_regime, y_spacing = generate_synthetic_data(100, 42, "test")

        m1, _, a1 = train_model(X, y_regime, y_spacing, 42)
        m2, _, a2 = train_model(X, y_regime, y_spacing, 42)

        # Same accuracy
        assert a1 == a2

        # Same predictions
        pred1 = m1.predict_proba(X)
        pred2 = m2.predict_proba(X)
        assert np.allclose(pred1, pred2)


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
class TestTrainAndExport:
    """Tests for full training pipeline."""

    def test_creates_artifact_directory(self) -> None:
        """Test that training creates artifact directory with required files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "artifact"
            train_and_export(out_dir, "test", n_samples=50)

            assert out_dir.exists()
            assert (out_dir / "model.onnx").exists()
            assert (out_dir / "manifest.json").exists()
            assert (out_dir / "train_report.json").exists()

    def test_manifest_valid(self) -> None:
        """Test that generated manifest is valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "artifact"
            train_and_export(out_dir, "test", n_samples=50)

            manifest = load_artifact(out_dir).manifest
            assert manifest.schema_version == "v1"
            assert manifest.model_file == "model.onnx"
            assert "model.onnx" in manifest.sha256

    def test_sha256_matches_model(self) -> None:
        """Test that manifest SHA256 matches actual model file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "artifact"
            train_and_export(out_dir, "test", n_samples=50)

            manifest_path = out_dir / "manifest.json"
            with manifest_path.open() as f:
                manifest = json.load(f)

            expected_sha = manifest["sha256"]["model.onnx"]
            actual_sha = compute_sha256(out_dir / "model.onnx")

            assert expected_sha == actual_sha

    def test_train_report_contains_metadata(self) -> None:
        """Test that train_report.json contains required metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "artifact"
            report = train_and_export(out_dir, "test", seed=42, n_samples=50)

            assert report.dataset_id == "test"
            assert report.seed == 42
            assert report.n_samples == 50
            assert report.n_features == len(FEATURE_ORDER)
            assert 0.0 <= report.train_accuracy <= 1.0
            assert "LOW" in report.regime_distribution
            assert "MID" in report.regime_distribution
            assert "HIGH" in report.regime_distribution

    def test_deterministic_artifacts(self) -> None:
        """Test that same parameters produce identical artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out1 = Path(tmpdir) / "art1"
            out2 = Path(tmpdir) / "art2"

            r1 = train_and_export(out1, "det", seed=42, n_samples=50)
            r2 = train_and_export(out2, "det", seed=42, n_samples=50)

            assert r1.model_sha256 == r2.model_sha256
            assert r1.train_accuracy == r2.train_accuracy

    def test_notes_in_manifest(self) -> None:
        """Test that notes are included in manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "artifact"
            train_and_export(out_dir, "test", n_samples=50, notes="Test notes")

            with (out_dir / "manifest.json").open() as f:
                manifest = json.load(f)

            assert manifest["notes"] == "Test notes"


@pytest.mark.skipif(not ONNX_AVAILABLE, reason="onnxruntime not installed")
class TestGoldenArtifact:
    """Tests using the golden test artifact."""

    @pytest.fixture
    def golden_artifact_dir(self) -> Path:
        """Path to golden test artifact."""
        return Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "golden_regime"

    def test_golden_artifact_exists(self, golden_artifact_dir: Path) -> None:
        """Test that golden artifact exists."""
        assert golden_artifact_dir.exists()
        assert (golden_artifact_dir / "model.onnx").exists()
        assert (golden_artifact_dir / "manifest.json").exists()
        assert (golden_artifact_dir / "train_report.json").exists()

    def test_golden_artifact_valid(self, golden_artifact_dir: Path) -> None:
        """Test that golden artifact passes validation."""
        artifact = load_artifact(golden_artifact_dir)
        assert artifact.manifest.schema_version == "v1"

    def test_golden_artifact_inference(self, golden_artifact_dir: Path) -> None:
        """Test inference with golden artifact."""
        from grinder.ml.onnx import OnnxMlModel  # noqa: PLC0415

        model = OnnxMlModel.load_from_dir(golden_artifact_dir)
        result = model.predict(
            ts_ms=1000,
            symbol="BTCUSDT",
            policy_features={"price_mid": 50000.0, "spread_bps": 5},
        )

        assert result is not None
        assert result.predicted_regime in ("LOW", "MID", "HIGH")
        assert sum(result.regime_probs_bps.values()) == 10000

    def test_golden_artifact_sha256_stable(self, golden_artifact_dir: Path) -> None:
        """Test that golden artifact SHA256 matches expected value.

        This test ensures the golden artifact is not accidentally modified.
        If this fails, either the artifact was modified or the training
        pipeline changed. Update the expected SHA256 if intentional.
        """
        actual_sha = compute_sha256(golden_artifact_dir / "model.onnx")

        # Read expected SHA from manifest
        with (golden_artifact_dir / "manifest.json").open() as f:
            manifest = json.load(f)
        expected_sha = manifest["sha256"]["model.onnx"]

        assert actual_sha == expected_sha, (
            f"Golden artifact SHA256 mismatch. "
            f"Expected: {expected_sha[:16]}..., Actual: {actual_sha[:16]}..."
        )


class TestComputeSha256:
    """Tests for SHA256 computation."""

    def test_computes_correct_hash(self) -> None:
        """Test SHA256 computation against known value."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            f.flush()

            expected = hashlib.sha256(b"test content").hexdigest()
            actual = compute_sha256(Path(f.name))

            assert actual == expected

    def test_hash_is_lowercase_hex(self) -> None:
        """Test that hash is lowercase 64-char hex."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"content")
            f.flush()

            result = compute_sha256(Path(f.name))

            assert len(result) == 64
            assert result.islower()
            assert all(c in "0123456789abcdef" for c in result)
