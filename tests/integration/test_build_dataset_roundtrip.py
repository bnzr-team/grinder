"""Integration test: build_dataset -> verify_dataset roundtrip.

M8-04b: Build a dataset with build_dataset, then verify it with verify_dataset.
End-to-end proof that the builder produces spec-compliant artifacts.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyarrow", reason="pyarrow not installed (ml extra)")

import pyarrow.parquet as pq
from scripts.build_dataset import build_dataset
from scripts.verify_dataset import verify_dataset

if TYPE_CHECKING:
    from pathlib import Path

FIXED_TS = "2026-02-17T12:00:00Z"


class TestBuildVerifyRoundtrip:
    """build_dataset output passes verify_dataset."""

    def test_roundtrip_default_params(self, tmp_path: Path) -> None:
        """Build with defaults and verify passes."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id="roundtrip_ds",
            source="synthetic",
            rows=50,
            seed=42,
            created_at_utc=FIXED_TS,
        )
        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=False)
        assert errors == [], f"Verification errors: {errors}"

    def test_roundtrip_large_dataset(self, tmp_path: Path) -> None:
        """Build with more rows and verify passes."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id="large_ds",
            source="synthetic",
            rows=500,
            seed=123,
            created_at_utc=FIXED_TS,
        )
        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=False)
        assert errors == [], f"Verification errors: {errors}"

    def test_roundtrip_verbose(self, tmp_path: Path) -> None:
        """Build + verify both with verbose=True."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id="verbose_ds",
            source="synthetic",
            rows=50,
            seed=42,
            verbose=True,
            created_at_utc=FIXED_TS,
        )
        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=True)
        assert errors == [], f"Verification errors: {errors}"

    def test_manifest_row_count_matches_parquet(self, tmp_path: Path) -> None:
        """Manifest row_count matches actual parquet rows."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id="count_ds",
            source="synthetic",
            rows=77,
            seed=42,
            created_at_utc=FIXED_TS,
        )
        manifest = json.loads((ds_dir / "manifest.json").read_text())
        table = pq.read_table(ds_dir / "data.parquet")
        assert manifest["row_count"] == table.num_rows == 77

    def test_force_rebuild_then_verify(self, tmp_path: Path) -> None:
        """Build, force-rebuild, verify the second build."""
        build_dataset(
            out_dir=tmp_path,
            dataset_id="force_ds",
            source="synthetic",
            rows=50,
            seed=42,
            created_at_utc=FIXED_TS,
        )
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id="force_ds",
            source="synthetic",
            rows=100,
            seed=99,
            force=True,
            created_at_utc=FIXED_TS,
        )
        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=False)
        assert errors == [], f"Verification errors: {errors}"

        manifest = json.loads(manifest_path.read_text())
        assert manifest["row_count"] == 100
