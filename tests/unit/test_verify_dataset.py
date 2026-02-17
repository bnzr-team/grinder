"""Unit tests for scripts/verify_dataset.py manifest validator.

M8-04a: Tests schema validation, path safety, SHA256 integrity,
feature_order_hash verification, and edge cases.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts.verify_dataset import (
    DATASET_ID_PATTERN,
    SCHEMA_VERSION,
    VALID_SOURCES,
    DatasetValidationError,
    _check_containment,
    _check_no_symlink,
    _compute_feature_order_hash,
    _get_ssot_feature_order,
    _validate_path_safety,
    verify_dataset,
)

from grinder.ml.onnx.features import FEATURE_ORDER

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, manifest: dict[str, object], data: bytes = b"hello") -> Path:
    """Write a manifest + data file and return manifest path."""
    ds_id = str(manifest.get("dataset_id", "test_ds"))
    ds_dir = tmp_path / ds_id
    ds_dir.mkdir(parents=True, exist_ok=True)

    # Write data file
    data_path = ds_dir / "data.parquet"
    data_path.write_bytes(data)
    data_sha = hashlib.sha256(data).hexdigest()

    # Inject correct SHA if not already populated
    if not manifest.get("sha256"):
        manifest["sha256"] = {"data.parquet": data_sha}

    manifest_path = ds_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def _valid_manifest(dataset_id: str = "test_dataset") -> dict[str, object]:
    """Return a minimal valid manifest dict."""
    foh = hashlib.sha256(json.dumps(list(FEATURE_ORDER)).encode()).hexdigest()[:16]
    return {
        "schema_version": "v1",
        "dataset_id": dataset_id,
        "created_at_utc": "2026-02-17T10:00:00Z",
        "source": "synthetic",
        "feature_order": list(FEATURE_ORDER),
        "feature_order_hash": foh,
        "label_columns": ["regime"],
        "row_count": 50,
        "sha256": {},  # Will be filled by _write_manifest
    }


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    """schema_version validation."""

    def test_valid_v1(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert errors == []

    def test_invalid_version(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["schema_version"] = "v2"
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("schema_version" in e for e in errors)

    def test_missing_version(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        del m["schema_version"]
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("Missing required keys" in e for e in errors)


# ---------------------------------------------------------------------------
# dataset_id
# ---------------------------------------------------------------------------


class TestDatasetId:
    """dataset_id pattern validation."""

    @pytest.mark.parametrize(
        "did",
        [
            "market_data_2025",
            "synthetic_v1",
            "golden.regime.v1",
            "abc",
            "a00",
        ],
    )
    def test_valid_ids(self, did: str) -> None:
        assert DATASET_ID_PATTERN.match(did) is not None

    @pytest.mark.parametrize(
        "did",
        [
            "My Dataset",  # uppercase + space
            "../escape",  # traversal
            "/absolute",  # absolute
            "a",  # too short (need 3+ total)
            "ab",  # too short
            "A_UPPER",  # uppercase
            "",  # empty
        ],
    )
    def test_invalid_ids(self, did: str) -> None:
        assert DATASET_ID_PATTERN.match(did) is None

    def test_dir_name_mismatch(self, tmp_path: Path) -> None:
        m = _valid_manifest("my_dataset")
        # Write to directory with wrong name
        ds_dir = tmp_path / "wrong_name"
        ds_dir.mkdir()
        data = b"hello"
        (ds_dir / "data.parquet").write_bytes(data)
        m["sha256"] = {"data.parquet": hashlib.sha256(data).hexdigest()}
        (ds_dir / "manifest.json").write_text(json.dumps(m))
        errors = verify_dataset(ds_dir / "manifest.json", base_dir=tmp_path)
        assert any("does not match" in e for e in errors)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class TestPathSafety:
    """Path traversal and absolute path rejection."""

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(DatasetValidationError, match="Absolute path"):
            _validate_path_safety("/etc/passwd")

    def test_traversal_rejected(self) -> None:
        with pytest.raises(DatasetValidationError, match="traversal"):
            _validate_path_safety("../../../etc/passwd")

    def test_relative_safe(self) -> None:
        _validate_path_safety("data.parquet")
        _validate_path_safety("subdir/file.txt")

    def test_sha256_key_traversal(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["sha256"] = {"../../../etc/passwd": "a" * 64}
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("traversal" in e.lower() or "path" in e.lower() for e in errors)

    def test_sha256_key_absolute(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["sha256"] = {"/etc/passwd": "a" * 64}
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("Absolute" in e or "absolute" in e.lower() for e in errors)

    def test_containment_escape(self, tmp_path: Path) -> None:
        parent = tmp_path / "parent"
        parent.mkdir()
        child = (tmp_path / "outside" / "file.txt").resolve()
        with pytest.raises(DatasetValidationError, match="escapes"):
            _check_containment(child, parent)


# ---------------------------------------------------------------------------
# feature_order_hash
# ---------------------------------------------------------------------------


class TestFeatureOrderHash:
    """feature_order_hash validation against FEATURE_ORDER SSOT."""

    def test_correct_hash_passes(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert errors == []

    def test_wrong_hash_fails(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["feature_order_hash"] = "deadbeef12345678"
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("feature_order_hash mismatch" in e for e in errors)

    def test_hash_matches_ssot(self) -> None:
        expected = hashlib.sha256(json.dumps(list(FEATURE_ORDER)).encode()).hexdigest()[:16]
        assert _compute_feature_order_hash() == expected


# ---------------------------------------------------------------------------
# SHA256 integrity
# ---------------------------------------------------------------------------


class TestSha256Integrity:
    """SHA256 checksum verification."""

    def test_correct_sha_passes(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m, data=b"test data 123")
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert errors == []

    def test_sha_mismatch_fails(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        data = b"original data"
        mp = _write_manifest(tmp_path, m, data=data)
        # Corrupt the data file after writing manifest
        ds_dir = mp.parent
        (ds_dir / "data.parquet").write_bytes(b"corrupted data")
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("SHA256 mismatch" in e for e in errors)

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        data = b"some data"
        m["sha256"] = {
            "data.parquet": hashlib.sha256(data).hexdigest(),
            "splits.json": "a" * 64,  # referenced but won't exist
        }
        ds_dir = tmp_path / str(m["dataset_id"])
        ds_dir.mkdir(parents=True)
        (ds_dir / "data.parquet").write_bytes(data)
        (ds_dir / "manifest.json").write_text(json.dumps(m))
        errors = verify_dataset(ds_dir / "manifest.json", base_dir=tmp_path)
        assert any("File not found" in e and "splits.json" in e for e in errors)

    def test_invalid_sha_format(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["sha256"] = {"data.parquet": "not-a-valid-sha"}
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("Invalid SHA256 value" in e for e in errors)


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------


class TestSource:
    """source enum validation."""

    @pytest.mark.parametrize("src", sorted(VALID_SOURCES))
    def test_valid_sources(self, src: str, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["source"] = src
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert not any("source" in e.lower() for e in errors)

    def test_invalid_source(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["source"] = "unknown"
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("Invalid source" in e for e in errors)


# ---------------------------------------------------------------------------
# Row count limits
# ---------------------------------------------------------------------------


class TestRowCount:
    """row_count limit validation."""

    def test_below_min(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["row_count"] = 5
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("below minimum" in e for e in errors)

    def test_above_max(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["row_count"] = 20_000_000
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("exceeds maximum" in e for e in errors)

    def test_not_int(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["row_count"] = "fifty"
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("must be int" in e for e in errors)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case handling."""

    def test_nonexistent_path(self) -> None:
        errors = verify_dataset(Path("/nonexistent/manifest.json"))
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad" / "manifest.json"
        p.parent.mkdir(parents=True)
        p.write_text("{{not json}}")
        errors = verify_dataset(p)
        assert any("Invalid JSON" in e for e in errors)

    def test_created_at_utc_must_end_z(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["created_at_utc"] = "2026-02-17T10:00:00+05:00"
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("created_at_utc" in e for e in errors)

    def test_schema_version_constant(self) -> None:
        assert SCHEMA_VERSION == "v1"


# ---------------------------------------------------------------------------
# feature_order list equality (not just hash)
# ---------------------------------------------------------------------------


class TestFeatureOrderListEquality:
    """feature_order list must exactly match SSOT FEATURE_ORDER."""

    def test_correct_list_passes(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert not any("feature_order mismatch" in e for e in errors)

    def test_wrong_order_fails(self, tmp_path: Path) -> None:
        """Swapped features: hash will differ AND list won't match."""
        m = _valid_manifest()
        fo = list(FEATURE_ORDER)
        fo[0], fo[1] = fo[1], fo[0]  # swap first two
        m["feature_order"] = fo
        # Recompute hash for the wrong list to isolate list check from hash check
        wrong_hash = hashlib.sha256(json.dumps(fo).encode()).hexdigest()[:16]
        m["feature_order_hash"] = wrong_hash
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("feature_order mismatch" in e for e in errors)

    def test_extra_feature_fails(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        fo = [*list(FEATURE_ORDER), "extra_feature"]
        m["feature_order"] = fo
        wrong_hash = hashlib.sha256(json.dumps(fo).encode()).hexdigest()[:16]
        m["feature_order_hash"] = wrong_hash
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("feature_order mismatch" in e for e in errors)

    def test_missing_feature_fails(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        fo = list(FEATURE_ORDER)[:-1]  # drop last
        m["feature_order"] = fo
        wrong_hash = hashlib.sha256(json.dumps(fo).encode()).hexdigest()[:16]
        m["feature_order_hash"] = wrong_hash
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("feature_order mismatch" in e for e in errors)

    def test_ssot_helper_returns_list(self) -> None:
        ssot = _get_ssot_feature_order()
        assert isinstance(ssot, list)
        assert ssot == list(FEATURE_ORDER)


# ---------------------------------------------------------------------------
# Required files (data.parquet)
# ---------------------------------------------------------------------------


class TestRequiredFiles:
    """data.parquet MUST exist in dataset directory (spec S3)."""

    def test_data_parquet_present_passes(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert not any("Required file missing" in e for e in errors)

    def test_data_parquet_missing_fails(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m)
        # Remove data.parquet after writing manifest
        (mp.parent / "data.parquet").unlink()
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("Required file missing: data.parquet" in e for e in errors)


# ---------------------------------------------------------------------------
# Symlink protection
# ---------------------------------------------------------------------------


class TestSymlinkProtection:
    """Symlinks must be rejected."""

    def test_symlink_helper_rejects(self, tmp_path: Path) -> None:
        real = tmp_path / "real.txt"
        real.write_bytes(b"data")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        with pytest.raises(DatasetValidationError, match=r"[Ss]ymlink"):
            _check_no_symlink(link)

    def test_non_symlink_passes(self, tmp_path: Path) -> None:
        real = tmp_path / "real.txt"
        real.write_bytes(b"data")
        _check_no_symlink(real)  # should not raise

    def test_sha256_symlink_file_rejected(self, tmp_path: Path) -> None:
        """Symlink in sha256 map should fail verification."""
        m = _valid_manifest()
        ds_dir = tmp_path / str(m["dataset_id"])
        ds_dir.mkdir(parents=True)

        # Create real data.parquet
        data = b"real data"
        (ds_dir / "data.parquet").write_bytes(data)
        data_sha = hashlib.sha256(data).hexdigest()

        # Create a symlink posing as splits.json
        target = tmp_path / "evil_target.txt"
        target.write_bytes(b"evil")
        target_sha = hashlib.sha256(b"evil").hexdigest()
        (ds_dir / "splits.json").symlink_to(target)

        m["sha256"] = {
            "data.parquet": data_sha,
            "splits.json": target_sha,
        }
        (ds_dir / "manifest.json").write_text(json.dumps(m))
        errors = verify_dataset(ds_dir / "manifest.json", base_dir=tmp_path)
        assert any("ymlink" in e for e in errors)

    def test_required_file_symlink_rejected(self, tmp_path: Path) -> None:
        """data.parquet as symlink must be rejected."""
        m = _valid_manifest()
        ds_dir = tmp_path / str(m["dataset_id"])
        ds_dir.mkdir(parents=True)

        # Create real file elsewhere and symlink as data.parquet
        real = tmp_path / "real_data.bin"
        real.write_bytes(b"data")
        (ds_dir / "data.parquet").symlink_to(real)

        m["sha256"] = {"data.parquet": hashlib.sha256(b"data").hexdigest()}
        (ds_dir / "manifest.json").write_text(json.dumps(m))
        errors = verify_dataset(ds_dir / "manifest.json", base_dir=tmp_path)
        assert any("symlink" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# created_at_utc (strict ISO 8601)
# ---------------------------------------------------------------------------


class TestCreatedAtUtc:
    """created_at_utc must be strict ISO 8601 UTC."""

    @pytest.mark.parametrize(
        "ts",
        [
            "2026-02-17T10:00:00Z",
            "2026-02-17T10:00:00.000Z",
            "2026-02-17T10:00:00.123456Z",
        ],
    )
    def test_valid_timestamps(self, ts: str, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["created_at_utc"] = ts
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert not any("created_at_utc" in e for e in errors)

    @pytest.mark.parametrize(
        "ts",
        [
            "2026-02-17T10:00:00+05:00",  # offset, not Z
            "2026-02-17",  # date only
            "not-a-timestamp",  # garbage
            "2026-02-17 10:00:00Z",  # space instead of T
            "2026/02/17T10:00:00Z",  # slashes
        ],
    )
    def test_invalid_timestamps(self, ts: str, tmp_path: Path) -> None:
        m = _valid_manifest()
        m["created_at_utc"] = ts
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert any("created_at_utc" in e for e in errors)


# ---------------------------------------------------------------------------
# Total directory size limit
# ---------------------------------------------------------------------------


class TestDirSizeLimit:
    """Total dataset directory size must not exceed 200 MB."""

    def test_small_dir_passes(self, tmp_path: Path) -> None:
        m = _valid_manifest()
        mp = _write_manifest(tmp_path, m)
        errors = verify_dataset(mp, base_dir=tmp_path)
        assert not any("directory size" in e.lower() for e in errors)
