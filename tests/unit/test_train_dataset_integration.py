"""Unit tests for M8-04c: train_regime_model dataset manifest integration.

Tests that training accepts --dataset-manifest, verifies it fail-closed,
loads data.parquet, and threads dataset_id into ONNX artifact manifest.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import numpy as np
import pytest

pytest.importorskip("pyarrow", reason="pyarrow not installed (ml extra)")

import pyarrow.parquet as pq
from scripts.build_dataset import build_dataset
from scripts.train_regime_model import (
    create_manifest,
    load_dataset_artifact,
    train_and_export,
)

from grinder.ml.onnx.features import FEATURE_ORDER

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_ds(tmp_path: Path, dataset_id: str = "train_ds", rows: int = 100) -> Path:
    """Build a dataset artifact and return manifest path."""
    ds_dir = build_dataset(
        out_dir=tmp_path / "datasets",
        dataset_id=dataset_id,
        source="synthetic",
        rows=rows,
        seed=42,
        created_at_utc=FIXED_TS,
    )
    return ds_dir / "manifest.json"


# ---------------------------------------------------------------------------
# load_dataset_artifact
# ---------------------------------------------------------------------------


class TestLoadDatasetArtifact:
    """load_dataset_artifact verifies and loads data correctly."""

    def test_loads_valid_dataset(self, tmp_path: Path) -> None:
        manifest_path = _build_ds(tmp_path)
        X, y_regime, y_spacing, dataset_id, row_count = load_dataset_artifact(manifest_path)

        assert X.shape == (100, len(FEATURE_ORDER))
        assert y_regime.shape == (100,)
        assert y_spacing.shape == (100,)
        assert dataset_id == "train_ds"
        assert row_count == 100

    def test_dataset_id_from_manifest(self, tmp_path: Path) -> None:
        manifest_path = _build_ds(tmp_path, dataset_id="custom_id")
        _, _, _, dataset_id, _ = load_dataset_artifact(manifest_path)
        assert dataset_id == "custom_id"

    def test_row_count_matches(self, tmp_path: Path) -> None:
        manifest_path = _build_ds(tmp_path, rows=77)
        _, _, _, _, row_count = load_dataset_artifact(manifest_path)
        assert row_count == 77

    def test_features_are_float32(self, tmp_path: Path) -> None:
        manifest_path = _build_ds(tmp_path, rows=50)
        X, _, _, _, _ = load_dataset_artifact(manifest_path)
        assert X.dtype == np.float32


# ---------------------------------------------------------------------------
# Fail on invalid dataset manifest
# ---------------------------------------------------------------------------


class TestTrainFailsOnInvalidDatasetManifest:
    """Training fails with clear error on corrupted dataset."""

    def test_fails_on_corrupted_sha(self, tmp_path: Path) -> None:
        """Tampered SHA256 in manifest triggers verify_dataset failure."""
        manifest_path = _build_ds(tmp_path)

        # Corrupt the SHA256 in manifest
        manifest = json.loads(manifest_path.read_text())
        manifest["sha256"]["data.parquet"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, indent=2))

        with pytest.raises(RuntimeError, match="verification failed"):
            load_dataset_artifact(manifest_path)

    def test_fails_on_missing_manifest(self, tmp_path: Path) -> None:
        """Non-existent manifest path raises error."""
        fake_path = tmp_path / "nonexistent" / "manifest.json"
        with pytest.raises((RuntimeError, FileNotFoundError, json.JSONDecodeError)):
            load_dataset_artifact(fake_path)


# ---------------------------------------------------------------------------
# Fail on missing feature column
# ---------------------------------------------------------------------------


class TestTrainFailsOnMissingFeatureColumn:
    """Training fails if dataset is missing a required feature column."""

    def test_fails_on_missing_feature(self, tmp_path: Path) -> None:
        """Dataset without a FEATURE_ORDER column is rejected."""
        manifest_path = _build_ds(tmp_path)
        ds_dir = manifest_path.parent

        # Read parquet, drop a column, rewrite
        table = pq.read_table(ds_dir / "data.parquet")
        # Drop the first feature column
        dropped_col = FEATURE_ORDER[0]
        new_table = table.drop(dropped_col)
        pq.write_table(new_table, ds_dir / "data.parquet", compression="snappy")

        # Update SHA to match new file (so verify_dataset SHA check passes)
        sha = hashlib.sha256((ds_dir / "data.parquet").read_bytes()).hexdigest()
        manifest = json.loads(manifest_path.read_text())
        manifest["sha256"]["data.parquet"] = sha
        manifest_path.write_text(json.dumps(manifest, indent=2))

        with pytest.raises(RuntimeError, match="Missing feature columns"):
            load_dataset_artifact(manifest_path)


# ---------------------------------------------------------------------------
# create_manifest writes dataset_id
# ---------------------------------------------------------------------------


class TestManifestWritesDatasetId:
    """create_manifest threads dataset_id into ONNX artifact manifest."""

    def test_dataset_id_in_manifest(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "artifact"
        out_dir.mkdir()
        # Create a dummy model file
        (out_dir / "model.onnx").write_bytes(b"fake")
        sha = hashlib.sha256(b"fake").hexdigest()

        manifest = create_manifest(out_dir, sha, dataset_id="my_ds")
        assert manifest["dataset_id"] == "my_ds"

        # Also check on-disk
        on_disk = json.loads((out_dir / "manifest.json").read_text())
        assert on_disk["dataset_id"] == "my_ds"

    def test_no_dataset_id_when_none(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "artifact"
        out_dir.mkdir()
        (out_dir / "model.onnx").write_bytes(b"fake")
        sha = hashlib.sha256(b"fake").hexdigest()

        manifest = create_manifest(out_dir, sha)
        assert "dataset_id" not in manifest


# ---------------------------------------------------------------------------
# train_and_export with dataset_manifest
# ---------------------------------------------------------------------------


@_needs_ml
class TestTrainAndExportWithManifest:
    """train_and_export with dataset_manifest loads from artifact."""

    def test_train_writes_dataset_id_to_model_manifest(self, tmp_path: Path) -> None:
        manifest_path = _build_ds(tmp_path, dataset_id="traced_ds", rows=100)

        out_dir = tmp_path / "model_artifact"
        report = train_and_export(
            out_dir=out_dir,
            dataset_id="",  # overridden by manifest
            dataset_manifest=manifest_path,
        )

        assert report.dataset_id == "traced_ds"
        assert report.n_samples == 100

        # Check model manifest has dataset_id
        model_manifest = json.loads((out_dir / "manifest.json").read_text())
        assert model_manifest["dataset_id"] == "traced_ds"

    def test_train_report_matches_dataset(self, tmp_path: Path) -> None:
        manifest_path = _build_ds(tmp_path, dataset_id="report_ds", rows=80)

        out_dir = tmp_path / "model_artifact"
        report = train_and_export(
            out_dir=out_dir,
            dataset_id="",
            dataset_manifest=manifest_path,
        )

        assert report.dataset_id == "report_ds"
        assert report.n_samples == 80
        assert report.n_features == len(FEATURE_ORDER)
        assert 0.0 <= report.train_accuracy <= 1.0
