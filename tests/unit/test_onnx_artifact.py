"""Tests for M8-02a ONNX artifact loader.

Tests artifact loading, manifest validation, and SHA256 integrity checks.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from grinder.ml.onnx import (
    ARTIFACT_SCHEMA_VERSION,
    OnnxArtifact,
    OnnxArtifactManifest,
    OnnxChecksumError,
    OnnxManifestError,
    OnnxPathError,
    load_artifact,
    load_manifest,
)


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

            assert manifest.schema_version == ARTIFACT_SCHEMA_VERSION
            assert manifest.model_file == "model.onnx"
