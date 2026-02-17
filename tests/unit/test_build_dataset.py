"""Unit tests for scripts/build_dataset.py dataset builder.

M8-04b: Tests layout, deterministic manifest, force overwrite,
feature_order SSOT compliance, and self-verification.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyarrow", reason="pyarrow not installed (ml extra)")

from scripts.build_dataset import (
    LABEL_COLUMNS,
    VALID_SOURCES,
    _build_manifest,
    _compute_feature_order_hash,
    _generate_synthetic_data,
    build_dataset,
)
from scripts.verify_dataset import verify_dataset

from grinder.ml.onnx.features import FEATURE_ORDER

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXED_TS = "2026-02-17T12:00:00Z"


def _build(tmp_path: Path, **kwargs: object) -> Path:
    """Build a dataset with sensible defaults, returning dataset_dir."""
    defaults = {
        "out_dir": tmp_path,
        "dataset_id": "test_ds",
        "source": "synthetic",
        "rows": 50,
        "seed": 42,
        "force": False,
        "created_at_utc": FIXED_TS,
        "verbose": False,
    }
    defaults.update(kwargs)
    return build_dataset(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestBuildDatasetCreatesLayout:
    """build_dataset produces data.parquet + manifest.json in <out_dir>/<dataset_id>/."""

    def test_creates_dataset_dir(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        assert ds_dir.is_dir()
        assert ds_dir.name == "test_ds"

    def test_creates_data_parquet(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        data_path = ds_dir / "data.parquet"
        assert data_path.exists()
        assert data_path.stat().st_size > 0

    def test_creates_manifest_json(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest_path = ds_dir / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["schema_version"] == "v1"
        assert manifest["dataset_id"] == "test_ds"

    def test_only_two_files(self, tmp_path: Path) -> None:
        """Dataset dir contains exactly data.parquet and manifest.json."""
        ds_dir = _build(tmp_path)
        files = sorted(f.name for f in ds_dir.iterdir())
        assert files == ["data.parquet", "manifest.json"]


# ---------------------------------------------------------------------------
# Deterministic manifest
# ---------------------------------------------------------------------------


class TestManifestIsDeterministicSortKeys:
    """manifest.json uses sort_keys=True for deterministic output."""

    def test_keys_are_sorted(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest_text = (ds_dir / "manifest.json").read_text()
        manifest = json.loads(manifest_text)
        keys = list(manifest.keys())
        assert keys == sorted(keys), f"Keys not sorted: {keys}"

    def test_determinism_block_keys_sorted(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest = json.loads((ds_dir / "manifest.json").read_text())
        det = manifest.get("determinism", {})
        if det:
            keys = list(det.keys())
            assert keys == sorted(keys), f"Determinism keys not sorted: {keys}"

    def test_same_seed_same_manifest(self, tmp_path: Path) -> None:
        """Two builds with same seed produce identical manifest (excluding timestamp)."""
        dir1 = _build(tmp_path, dataset_id="ds_a", seed=99, created_at_utc=FIXED_TS)
        dir2 = _build(tmp_path, dataset_id="ds_b", seed=99, created_at_utc=FIXED_TS)

        m1 = json.loads((dir1 / "manifest.json").read_text())
        m2 = json.loads((dir2 / "manifest.json").read_text())

        # Normalize fields that differ by design
        for m in (m1, m2):
            m.pop("dataset_id", None)
            m.pop("determinism", None)

        assert m1 == m2

    def test_sha256_map_present(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest = json.loads((ds_dir / "manifest.json").read_text())
        sha_map = manifest["sha256"]
        assert "data.parquet" in sha_map
        assert len(sha_map["data.parquet"]) == 64  # SHA256 hex digest


# ---------------------------------------------------------------------------
# Force overwrite
# ---------------------------------------------------------------------------


class TestForceOverwrite:
    """--force behavior."""

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        _build(tmp_path, dataset_id="dup_ds")
        with pytest.raises(FileExistsError, match="already exists"):
            _build(tmp_path, dataset_id="dup_ds", force=False)

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        ds_dir1 = _build(tmp_path, dataset_id="dup_ds", rows=50)
        assert ds_dir1.exists()

        ds_dir2 = _build(tmp_path, dataset_id="dup_ds", rows=100, force=True)
        assert ds_dir2.exists()

        manifest = json.loads((ds_dir2 / "manifest.json").read_text())
        assert manifest["row_count"] == 100

    def test_force_cleans_old_files(self, tmp_path: Path) -> None:
        """--force removes all old files before rebuild."""
        ds_dir = _build(tmp_path, dataset_id="clean_ds")
        # Plant an extra file
        (ds_dir / "stale.txt").write_text("old")

        _build(tmp_path, dataset_id="clean_ds", force=True)
        files = sorted(f.name for f in ds_dir.iterdir())
        assert "stale.txt" not in files
        assert files == ["data.parquet", "manifest.json"]


# ---------------------------------------------------------------------------
# Feature order SSOT
# ---------------------------------------------------------------------------


class TestFeatureOrderEqualsSsot:
    """feature_order in manifest must match FEATURE_ORDER from features.py."""

    def test_feature_order_matches_ssot(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest = json.loads((ds_dir / "manifest.json").read_text())
        assert manifest["feature_order"] == list(FEATURE_ORDER)

    def test_feature_order_hash_matches(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest = json.loads((ds_dir / "manifest.json").read_text())
        expected_hash = _compute_feature_order_hash()
        assert manifest["feature_order_hash"] == expected_hash

    def test_label_columns_present(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path)
        manifest = json.loads((ds_dir / "manifest.json").read_text())
        assert manifest["label_columns"] == list(LABEL_COLUMNS)


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------


class TestGenerateSyntheticData:
    """_generate_synthetic_data produces correct schema."""

    def test_column_count(self) -> None:
        table = _generate_synthetic_data(10, seed=42)
        expected_cols = len(FEATURE_ORDER) + len(LABEL_COLUMNS)
        assert table.num_columns == expected_cols

    def test_row_count(self) -> None:
        table = _generate_synthetic_data(100, seed=42)
        assert table.num_rows == 100

    def test_deterministic(self) -> None:
        t1 = _generate_synthetic_data(50, seed=7)
        t2 = _generate_synthetic_data(50, seed=7)
        assert t1.equals(t2)

    def test_different_seeds_differ(self) -> None:
        t1 = _generate_synthetic_data(50, seed=1)
        t2 = _generate_synthetic_data(50, seed=2)
        assert not t1.equals(t2)

    def test_feature_columns_present(self) -> None:
        table = _generate_synthetic_data(10, seed=42)
        col_names = table.column_names
        for feat in FEATURE_ORDER:
            assert feat in col_names, f"Missing feature: {feat}"

    def test_label_columns_present(self) -> None:
        table = _generate_synthetic_data(10, seed=42)
        col_names = table.column_names
        for label in LABEL_COLUMNS:
            assert label in col_names, f"Missing label: {label}"


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


class TestBuildManifest:
    """_build_manifest output."""

    def test_schema_version_v1(self, tmp_path: Path) -> None:
        ds_dir = tmp_path / "ds"
        ds_dir.mkdir()
        (ds_dir / "data.parquet").write_bytes(b"test")

        m = _build_manifest("ds", "synthetic", 10, 42, ds_dir, created_at_utc=FIXED_TS)
        assert m["schema_version"] == "v1"

    def test_determinism_block_for_synthetic(self, tmp_path: Path) -> None:
        ds_dir = tmp_path / "ds"
        ds_dir.mkdir()
        (ds_dir / "data.parquet").write_bytes(b"test")

        m = _build_manifest("ds", "synthetic", 10, 42, ds_dir, created_at_utc=FIXED_TS)
        det = m.get("determinism")
        assert det is not None
        assert det["seed"] == 42  # type: ignore[index]
        assert "build_command" in det  # type: ignore[operator]

    def test_no_determinism_for_non_synthetic(self, tmp_path: Path) -> None:
        ds_dir = tmp_path / "ds"
        ds_dir.mkdir()
        (ds_dir / "data.parquet").write_bytes(b"test")

        m = _build_manifest("ds", "backtest", 10, None, ds_dir, created_at_utc=FIXED_TS)
        assert "determinism" not in m


# ---------------------------------------------------------------------------
# Self-verification
# ---------------------------------------------------------------------------


class TestSelfVerification:
    """build_dataset self-verifies via verify_dataset."""

    def test_built_dataset_passes_verification(self, tmp_path: Path) -> None:
        """The built dataset must pass verify_dataset without errors."""
        ds_dir = _build(tmp_path)
        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=False)
        assert errors == [], f"Self-verification errors: {errors}"


# ---------------------------------------------------------------------------
# Valid sources constant
# ---------------------------------------------------------------------------


class TestValidSources:
    """VALID_SOURCES matches expected set."""

    def test_valid_sources(self) -> None:
        assert {"synthetic", "backtest", "export", "manual"} == VALID_SOURCES


# ---------------------------------------------------------------------------
# Verbose mode
# ---------------------------------------------------------------------------


class TestVerboseMode:
    """Verbose output does not break anything."""

    def test_verbose_build_succeeds(self, tmp_path: Path) -> None:
        ds_dir = _build(tmp_path, verbose=True)
        assert ds_dir.exists()
        assert (ds_dir / "data.parquet").exists()
        assert (ds_dir / "manifest.json").exists()
