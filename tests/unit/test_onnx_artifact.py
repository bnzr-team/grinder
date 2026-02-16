"""Tests for M8-02a/M8-03a ONNX artifact loader.

Tests artifact loading, manifest validation, SHA256 integrity checks,
and v1.1 schema extensions (feature_order, git_sha, dataset_id).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest

from grinder.ml.onnx import (
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_SCHEMA_VERSIONS,
    OnnxArtifact,
    OnnxArtifactManifest,
    OnnxChecksumError,
    OnnxManifestError,
    OnnxPathError,
    load_artifact,
    load_manifest,
)
from grinder.ml.onnx.artifact import validate_feature_order
from grinder.ml.onnx.features import FEATURE_ORDER


def _compute_sha256(data: bytes) -> str:
    """Helper to compute SHA256 of bytes."""
    return hashlib.sha256(data).hexdigest().lower()


def _create_artifact_dir(
    tmpdir: Path,
    model_content: bytes = b"fake onnx model content",
    extra_files: dict[str, bytes] | None = None,
    manifest_override: dict[str, Any] | None = None,
) -> Path:
    """Create a valid artifact directory for testing."""
    artifact_dir = tmpdir / "artifact"
    artifact_dir.mkdir()

    # Write model file
    model_path = artifact_dir / "model.onnx"
    model_path.write_bytes(model_content)

    # Write extra files
    sha256_map = {"model.onnx": _compute_sha256(model_content)}
    if extra_files:
        for name, content in extra_files.items():
            (artifact_dir / name).write_bytes(content)
            sha256_map[name] = _compute_sha256(content)

    # Write manifest
    manifest = {
        "schema_version": "v1",
        "model_file": "model.onnx",
        "sha256": sha256_map,
        "created_at": "2026-02-14T00:00:00Z",
        "notes": "Test artifact",
    }
    if manifest_override:
        manifest.update(manifest_override)

    (artifact_dir / "manifest.json").write_text(json.dumps(manifest))
    return artifact_dir


class TestOnnxArtifactManifest:
    """Tests for OnnxArtifactManifest validation."""

    def test_valid_manifest(self) -> None:
        """Test creating a valid manifest."""
        manifest = OnnxArtifactManifest(
            schema_version="v1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
            notes="Test",
        )
        assert manifest.schema_version == "v1"
        assert manifest.model_file == "model.onnx"

    def test_invalid_schema_version(self) -> None:
        """Test that invalid schema version is rejected."""
        with pytest.raises(OnnxManifestError, match="Unsupported schema_version"):
            OnnxArtifactManifest(
                schema_version="v2",
                model_file="model.onnx",
                sha256={"model.onnx": "a" * 64},
                created_at="2026-02-14T00:00:00Z",
            )

    def test_model_file_not_in_sha256(self) -> None:
        """Test that model_file must be in sha256 map."""
        with pytest.raises(OnnxManifestError, match="not found in sha256"):
            OnnxArtifactManifest(
                schema_version="v1",
                model_file="model.onnx",
                sha256={"other.onnx": "a" * 64},
                created_at="2026-02-14T00:00:00Z",
            )

    def test_empty_sha256_map(self) -> None:
        """Test that empty sha256 map is rejected."""
        with pytest.raises(OnnxManifestError, match="cannot be empty"):
            OnnxArtifactManifest(
                schema_version="v1",
                model_file="model.onnx",
                sha256={},
                created_at="2026-02-14T00:00:00Z",
            )

    def test_invalid_sha256_length(self) -> None:
        """Test that SHA256 must be 64 characters."""
        with pytest.raises(OnnxManifestError, match="64-char hex"):
            OnnxArtifactManifest(
                schema_version="v1",
                model_file="model.onnx",
                sha256={"model.onnx": "abc123"},  # Too short
                created_at="2026-02-14T00:00:00Z",
            )

    def test_invalid_sha256_hex(self) -> None:
        """Test that SHA256 must be valid hex."""
        with pytest.raises(OnnxManifestError, match="not a valid hex"):
            OnnxArtifactManifest(
                schema_version="v1",
                model_file="model.onnx",
                sha256={"model.onnx": "g" * 64},  # Invalid hex
                created_at="2026-02-14T00:00:00Z",
            )

    def test_from_dict_missing_fields(self) -> None:
        """Test that missing required fields raise error."""
        with pytest.raises(OnnxManifestError, match="Missing required fields"):
            OnnxArtifactManifest.from_dict({"schema_version": "v1"})

    def test_from_dict_valid(self) -> None:
        """Test creating manifest from dict."""
        manifest = OnnxArtifactManifest.from_dict(
            {
                "schema_version": "v1",
                "model_file": "model.onnx",
                "sha256": {"model.onnx": "a" * 64},
                "created_at": "2026-02-14T00:00:00Z",
            }
        )
        assert manifest.model_file == "model.onnx"
        assert manifest.notes is None


class TestLoadArtifact:
    """Tests for load_artifact() function."""

    def test_load_valid_artifact(self) -> None:
        """Test loading a valid artifact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _create_artifact_dir(Path(tmpdir))
            artifact = load_artifact(artifact_dir)

            assert isinstance(artifact, OnnxArtifact)
            assert artifact.manifest.schema_version == "v1"
            assert artifact.model_path.exists()

    def test_load_artifact_with_extra_files(self) -> None:
        """Test loading artifact with additional files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _create_artifact_dir(
                Path(tmpdir),
                extra_files={"config.json": b'{"key": "value"}'},
            )
            artifact = load_artifact(artifact_dir)
            assert len(artifact.manifest.sha256) == 2

    def test_missing_manifest(self) -> None:
        """Test that missing manifest.json raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()
            (artifact_dir / "model.onnx").write_bytes(b"data")

            with pytest.raises(OnnxManifestError, match=r"manifest\.json not found"):
                load_artifact(artifact_dir)

    def test_invalid_json_manifest(self) -> None:
        """Test that invalid JSON in manifest raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()
            (artifact_dir / "manifest.json").write_text("not valid json")

            with pytest.raises(OnnxManifestError, match="Invalid JSON"):
                load_artifact(artifact_dir)

    def test_missing_model_file(self) -> None:
        """Test that missing model file raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()

            manifest = {
                "schema_version": "v1",
                "model_file": "model.onnx",
                "sha256": {"model.onnx": "a" * 64},
                "created_at": "2026-02-14T00:00:00Z",
            }
            (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

            with pytest.raises(FileNotFoundError, match=r"model\.onnx"):
                load_artifact(artifact_dir)

    def test_sha256_mismatch(self) -> None:
        """Test that SHA256 mismatch raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()

            (artifact_dir / "model.onnx").write_bytes(b"actual content")

            manifest = {
                "schema_version": "v1",
                "model_file": "model.onnx",
                "sha256": {"model.onnx": "a" * 64},  # Wrong checksum
                "created_at": "2026-02-14T00:00:00Z",
            }
            (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

            with pytest.raises(OnnxChecksumError, match="SHA256 mismatch"):
                load_artifact(artifact_dir)

    def test_nonexistent_directory(self) -> None:
        """Test that nonexistent directory raises error."""
        with pytest.raises(OnnxManifestError, match="does not exist"):
            load_artifact("/nonexistent/path")


class TestPathSafety:
    """Tests for path traversal and safety checks."""

    def test_absolute_path_rejected(self) -> None:
        """Test that absolute paths in manifest are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()

            (artifact_dir / "model.onnx").write_bytes(b"content")
            model_sha = _compute_sha256(b"content")

            manifest = {
                "schema_version": "v1",
                "model_file": "/etc/passwd",
                "sha256": {"/etc/passwd": model_sha},
                "created_at": "2026-02-14T00:00:00Z",
            }
            (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

            with pytest.raises(OnnxPathError, match="Absolute paths not allowed"):
                load_artifact(artifact_dir)

    def test_path_traversal_rejected(self) -> None:
        """Test that path traversal is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()

            (artifact_dir / "model.onnx").write_bytes(b"content")
            model_sha = _compute_sha256(b"content")

            manifest = {
                "schema_version": "v1",
                "model_file": "../../../etc/passwd",
                "sha256": {"../../../etc/passwd": model_sha},
                "created_at": "2026-02-14T00:00:00Z",
            }
            (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

            with pytest.raises(OnnxPathError, match="Path traversal not allowed"):
                load_artifact(artifact_dir)

    def test_subdirectory_paths_allowed(self) -> None:
        """Test that subdirectory paths are allowed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifact"
            artifact_dir.mkdir()
            (artifact_dir / "models").mkdir()

            model_content = b"model in subdir"
            (artifact_dir / "models" / "model.onnx").write_bytes(model_content)
            model_sha = _compute_sha256(model_content)

            manifest = {
                "schema_version": "v1",
                "model_file": "models/model.onnx",
                "sha256": {"models/model.onnx": model_sha},
                "created_at": "2026-02-14T00:00:00Z",
            }
            (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

            artifact = load_artifact(artifact_dir)
            assert artifact.model_path.exists()


class TestLoadManifest:
    """Tests for load_manifest() function."""

    def test_load_manifest_directly(self) -> None:
        """Test loading manifest without full validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _create_artifact_dir(Path(tmpdir))
            manifest = load_manifest(artifact_dir)

            # _create_artifact_dir creates v1 artifacts by default
            assert manifest.schema_version == "v1"
            assert manifest.model_file == "model.onnx"


class TestManifestV11:
    """Tests for v1.1 manifest schema extensions (M8-03a)."""

    def test_schema_version_constants(self) -> None:
        """Test schema version constants are correctly defined."""
        assert ARTIFACT_SCHEMA_VERSION == "v1.1"
        assert ARTIFACT_SCHEMA_VERSIONS == ("v1", "v1.1")

    def test_valid_v11_manifest(self) -> None:
        """Test creating a valid v1.1 manifest with all optional fields."""
        manifest = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
            created_at_utc="2026-02-14T00:00:00Z",
            git_sha="b" * 40,
            dataset_id="train_2026Q1",
            feature_order=FEATURE_ORDER,
            notes="Test v1.1 manifest",
        )
        assert manifest.schema_version == "v1.1"
        assert manifest.git_sha == "b" * 40
        assert manifest.dataset_id == "train_2026Q1"
        assert manifest.feature_order == FEATURE_ORDER

    def test_v11_manifest_optional_fields_default_none(self) -> None:
        """Test that v1.1 optional fields default to None."""
        manifest = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
        )
        assert manifest.created_at_utc is None
        assert manifest.git_sha is None
        assert manifest.dataset_id is None
        assert manifest.feature_order is None

    def test_invalid_git_sha_length(self) -> None:
        """Test that git_sha must be exactly 40 characters."""
        with pytest.raises(OnnxManifestError, match="40-char hex"):
            OnnxArtifactManifest(
                schema_version="v1.1",
                model_file="model.onnx",
                sha256={"model.onnx": "a" * 64},
                created_at="2026-02-14T00:00:00Z",
                git_sha="abc123",  # Too short
            )

    def test_invalid_git_sha_hex(self) -> None:
        """Test that git_sha must be valid hex."""
        with pytest.raises(OnnxManifestError, match="not a valid hex"):
            OnnxArtifactManifest(
                schema_version="v1.1",
                model_file="model.onnx",
                sha256={"model.onnx": "a" * 64},
                created_at="2026-02-14T00:00:00Z",
                git_sha="g" * 40,  # Invalid hex
            )

    def test_from_dict_with_v11_fields(self) -> None:
        """Test creating v1.1 manifest from dict."""
        manifest = OnnxArtifactManifest.from_dict(
            {
                "schema_version": "v1.1",
                "model_file": "model.onnx",
                "sha256": {"model.onnx": "a" * 64},
                "created_at": "2026-02-14T00:00:00Z",
                "created_at_utc": "2026-02-14T00:00:00Z",
                "git_sha": "c" * 40,
                "dataset_id": "test_dataset",
                "feature_order": ["feat1", "feat2"],
            }
        )
        assert manifest.schema_version == "v1.1"
        assert manifest.git_sha == "c" * 40
        assert manifest.dataset_id == "test_dataset"
        assert manifest.feature_order == ("feat1", "feat2")

    def test_to_dict_roundtrip(self) -> None:
        """Test to_dict preserves all fields."""
        original = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "d" * 64},
            created_at="2026-02-14T12:00:00Z",
            created_at_utc="2026-02-14T12:00:00Z",
            git_sha="e" * 40,
            dataset_id="roundtrip_test",
            feature_order=("price_mid", "volume_24h"),
            notes="Roundtrip test",
        )

        d = original.to_dict()
        restored = OnnxArtifactManifest.from_dict(d)

        assert restored.schema_version == original.schema_version
        assert restored.git_sha == original.git_sha
        assert restored.dataset_id == original.dataset_id
        assert restored.feature_order == original.feature_order
        assert restored.notes == original.notes

    def test_to_dict_omits_none_fields(self) -> None:
        """Test that to_dict omits fields that are None."""
        manifest = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
        )

        d = manifest.to_dict()
        assert "git_sha" not in d
        assert "dataset_id" not in d
        assert "feature_order" not in d
        assert "created_at_utc" not in d

    def test_load_v11_artifact(self) -> None:
        """Test loading a v1.1 artifact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _create_artifact_dir(
                Path(tmpdir),
                manifest_override={
                    "schema_version": "v1.1",
                    "git_sha": "f" * 40,
                    "dataset_id": "test_v11",
                    "feature_order": list(FEATURE_ORDER),
                },
            )
            artifact = load_artifact(artifact_dir)

            assert artifact.manifest.schema_version == "v1.1"
            assert artifact.manifest.git_sha == "f" * 40
            assert artifact.manifest.dataset_id == "test_v11"
            assert artifact.manifest.feature_order == FEATURE_ORDER


class TestFeatureOrderValidation:
    """Tests for feature_order validation against SSOT (M8-03a)."""

    def test_matching_feature_order_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that matching feature_order doesn't log warning."""
        manifest = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
            feature_order=FEATURE_ORDER,
        )

        with caplog.at_level(logging.DEBUG):
            validate_feature_order(manifest)

        assert "FEATURE_ORDER_MISMATCH" not in caplog.text

    def test_missing_feature_order_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that missing feature_order (v1) doesn't log warning."""
        manifest = OnnxArtifactManifest(
            schema_version="v1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
            feature_order=None,
        )

        with caplog.at_level(logging.WARNING):
            validate_feature_order(manifest)

        assert "FEATURE_ORDER_MISMATCH" not in caplog.text

    def test_mismatched_feature_order_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that mismatched feature_order logs warning with details."""
        # Create manifest with different feature order
        different_features = ("custom_feat1", "custom_feat2")
        manifest = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
            feature_order=different_features,
        )

        with caplog.at_level(logging.WARNING):
            validate_feature_order(manifest)

        assert "FEATURE_ORDER_MISMATCH" in caplog.text
        assert "manifest_len=2" in caplog.text
        assert f"ssot_len={len(FEATURE_ORDER)}" in caplog.text

    def test_feature_order_order_mismatch_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that same features in different order logs warning."""
        # Same features, different order
        reversed_features = tuple(reversed(FEATURE_ORDER))
        manifest = OnnxArtifactManifest(
            schema_version="v1.1",
            model_file="model.onnx",
            sha256={"model.onnx": "a" * 64},
            created_at="2026-02-14T00:00:00Z",
            feature_order=reversed_features,
        )

        with caplog.at_level(logging.WARNING):
            validate_feature_order(manifest)

        assert "FEATURE_ORDER_MISMATCH" in caplog.text
        assert "order_differs=True" in caplog.text
