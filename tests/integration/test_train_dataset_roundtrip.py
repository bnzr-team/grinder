"""Integration test: build_dataset -> train_regime_model -> verify_onnx_artifact.

M8-04c: End-to-end proof that dataset artifacts flow through training
to produce valid ONNX artifacts with dataset_id traceability.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyarrow", reason="pyarrow not installed (ml extra)")

from scripts.build_dataset import build_dataset
from scripts.train_regime_model import train_and_export

from grinder.ml.onnx import load_artifact

try:
    import sklearn  # noqa: F401

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

try:
    import onnxruntime  # type: ignore  # noqa: F401

    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False

_needs_ml = pytest.mark.skipif(
    not (ONNX_AVAILABLE and SKLEARN_AVAILABLE),
    reason="onnxruntime and scikit-learn required (pip install grinder[ml])",
)

if TYPE_CHECKING:
    from pathlib import Path

FIXED_TS = "2026-02-17T12:00:00Z"


@_needs_ml
class TestBuildTrainVerifyRoundtrip:
    """build_dataset -> train_regime_model -> verify_onnx_artifact."""

    def test_full_roundtrip(self, tmp_path: Path) -> None:
        """Build dataset, train from it, verify ONNX artifact."""
        # 1. Build dataset
        ds_dir = build_dataset(
            out_dir=tmp_path / "datasets",
            dataset_id="roundtrip_ds",
            source="synthetic",
            rows=100,
            seed=42,
            created_at_utc=FIXED_TS,
        )
        ds_manifest = ds_dir / "manifest.json"

        # 2. Train from dataset artifact
        model_dir = tmp_path / "model_artifact"
        report = train_and_export(
            out_dir=model_dir,
            dataset_id="",
            dataset_manifest=ds_manifest,
        )

        # 3. Verify ONNX artifact
        artifact = load_artifact(model_dir)
        assert artifact.manifest.schema_version == "v1.1"
        assert "model.onnx" in artifact.manifest.sha256

        # 4. Check dataset_id traceability
        model_manifest = json.loads((model_dir / "manifest.json").read_text())
        ds_manifest_data = json.loads(ds_manifest.read_text())

        assert model_manifest["dataset_id"] == ds_manifest_data["dataset_id"]
        assert model_manifest["dataset_id"] == "roundtrip_ds"
        assert report.dataset_id == "roundtrip_ds"

    def test_roundtrip_different_dataset(self, tmp_path: Path) -> None:
        """Different dataset_id flows through correctly."""
        ds_dir = build_dataset(
            out_dir=tmp_path / "datasets",
            dataset_id="alt_dataset",
            source="synthetic",
            rows=50,
            seed=99,
            created_at_utc=FIXED_TS,
        )

        model_dir = tmp_path / "model_artifact"
        report = train_and_export(
            out_dir=model_dir,
            dataset_id="",
            dataset_manifest=ds_dir / "manifest.json",
        )

        model_manifest = json.loads((model_dir / "manifest.json").read_text())
        assert model_manifest["dataset_id"] == "alt_dataset"
        assert report.dataset_id == "alt_dataset"
        assert report.n_samples == 50
