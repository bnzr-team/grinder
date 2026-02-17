"""Integration test: golden dataset build -> verify roundtrip.

M8-04e: Generate a golden dataset in tmp_path using build_dataset,
verify with verify_dataset, and confirm deterministic output (same seed -> same SHA).
No committed parquet -- everything generated at test time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pyarrow", reason="pyarrow not installed (ml extra)")

from scripts.build_dataset import build_dataset
from scripts.verify_dataset import verify_dataset

if TYPE_CHECKING:
    from pathlib import Path

FIXED_TS = "2026-02-17T12:00:00Z"
GOLDEN_ID = "golden_tiny_v1"
GOLDEN_SEED = 1
GOLDEN_ROWS = 50


class TestGoldenDataset:
    """Build a golden dataset in tmp_path and verify it passes all checks."""

    def test_build_and_verify_pass(self, tmp_path: Path) -> None:
        """build_dataset -> verify_dataset PASS (golden tiny)."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id=GOLDEN_ID,
            source="synthetic",
            rows=GOLDEN_ROWS,
            seed=GOLDEN_SEED,
            created_at_utc=FIXED_TS,
        )
        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=False)
        assert errors == [], f"Golden dataset verification failed: {errors}"

    def test_golden_manifest_schema(self, tmp_path: Path) -> None:
        """Golden dataset manifest has all required fields."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id=GOLDEN_ID,
            source="synthetic",
            rows=GOLDEN_ROWS,
            seed=GOLDEN_SEED,
            created_at_utc=FIXED_TS,
        )
        manifest = json.loads((ds_dir / "manifest.json").read_text())

        assert manifest["schema_version"] == "v1"
        assert manifest["dataset_id"] == GOLDEN_ID
        assert manifest["source"] == "synthetic"
        assert manifest["row_count"] == GOLDEN_ROWS
        assert manifest["created_at_utc"] == FIXED_TS
        assert isinstance(manifest["feature_order"], list)
        assert len(manifest["feature_order"]) > 0
        assert isinstance(manifest["feature_order_hash"], str)
        assert isinstance(manifest["sha256"], dict)
        assert "data.parquet" in manifest["sha256"]

    def test_golden_deterministic_sha(self, tmp_path: Path) -> None:
        """Same seed + rows -> same parquet SHA256 (determinism)."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"

        ds_a = build_dataset(
            out_dir=dir_a,
            dataset_id=GOLDEN_ID,
            source="synthetic",
            rows=GOLDEN_ROWS,
            seed=GOLDEN_SEED,
            created_at_utc=FIXED_TS,
        )
        ds_b = build_dataset(
            out_dir=dir_b,
            dataset_id=GOLDEN_ID,
            source="synthetic",
            rows=GOLDEN_ROWS,
            seed=GOLDEN_SEED,
            created_at_utc=FIXED_TS,
        )

        manifest_a = json.loads((ds_a / "manifest.json").read_text())
        manifest_b = json.loads((ds_b / "manifest.json").read_text())

        sha_a = manifest_a["sha256"]["data.parquet"]
        sha_b = manifest_b["sha256"]["data.parquet"]
        assert sha_a == sha_b, f"Determinism failed: {sha_a} != {sha_b}"

    def test_corrupted_parquet_fails_verify(self, tmp_path: Path) -> None:
        """Corrupted data.parquet -> verify_dataset FAIL (fail-closed)."""
        ds_dir = build_dataset(
            out_dir=tmp_path,
            dataset_id=GOLDEN_ID,
            source="synthetic",
            rows=GOLDEN_ROWS,
            seed=GOLDEN_SEED,
            created_at_utc=FIXED_TS,
        )

        # Corrupt the parquet file (append 1 byte)
        parquet_path = ds_dir / "data.parquet"
        with parquet_path.open("ab") as f:
            f.write(b"X")

        manifest_path = ds_dir / "manifest.json"
        errors = verify_dataset(manifest_path, base_dir=tmp_path, verbose=False)
        assert len(errors) > 0, "Expected verification failure after corruption"
        assert any("SHA256" in e or "mismatch" in e for e in errors), (
            f"Expected SHA mismatch error, got: {errors}"
        )
