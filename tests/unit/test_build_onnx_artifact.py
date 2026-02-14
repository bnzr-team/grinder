"""Tests for M8-03a build_onnx_artifact script.

Tests artifact building, v1.1 manifest generation, and git_sha detection.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.build_onnx_artifact import build_artifact, compute_sha256, get_git_sha

from grinder.ml.onnx import load_artifact
from grinder.ml.onnx.features import FEATURE_ORDER
from grinder.ml.onnx.types import ARTIFACT_SCHEMA_VERSION


class TestComputeSha256:
    """Tests for compute_sha256 function."""

    def test_computes_correct_hash(self) -> None:
        """Test SHA256 computation matches expected value."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            f.flush()
            path = Path(f.name)

        try:
            sha = compute_sha256(path)
            # Known SHA256 of "test content"
            assert sha == "6ae8a75555209fd6c44157c0aed8016e763ff435a19cf186f76863140143ff72"
            assert len(sha) == 64
        finally:
            path.unlink()


class TestGetGitSha:
    """Tests for get_git_sha function."""

    def test_returns_sha_in_git_repo(self) -> None:
        """Test that git SHA is returned when in a git repo."""
        # This test may or may not pass depending on environment
        sha = get_git_sha()
        if sha is not None:
            assert len(sha) == 40
            assert all(c in "0123456789abcdef" for c in sha)

    def test_returns_none_when_git_unavailable(self) -> None:
        """Test graceful fallback when git is not available."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            sha = get_git_sha()
            assert sha is None

    def test_returns_none_on_timeout(self) -> None:
        """Test graceful fallback on subprocess timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            sha = get_git_sha()
            assert sha is None


class TestBuildArtifact:
    """Tests for build_artifact function."""

    def test_builds_valid_artifact(self) -> None:
        """Test building a valid v1.1 artifact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create source model
            model_path = tmpdir_path / "source_model.onnx"
            model_content = b"fake onnx model content for testing"
            model_path.write_bytes(model_content)

            # Build artifact
            output_dir = tmpdir_path / "artifact"
            artifact_dir = build_artifact(
                model_path=model_path,
                output_dir=output_dir,
                dataset_id="test_dataset_v1",
                notes="Test artifact",
            )

            # Verify directory created
            assert artifact_dir.exists()
            assert artifact_dir.is_dir()

            # Verify model copied
            assert (artifact_dir / "model.onnx").exists()
            assert (artifact_dir / "model.onnx").read_bytes() == model_content

            # Verify manifest
            manifest_path = artifact_dir / "manifest.json"
            assert manifest_path.exists()

            with manifest_path.open() as f:
                manifest = json.load(f)

            assert manifest["schema_version"] == ARTIFACT_SCHEMA_VERSION
            assert manifest["model_file"] == "model.onnx"
            assert manifest["dataset_id"] == "test_dataset_v1"
            assert manifest["notes"] == "Test artifact"
            assert manifest["feature_order"] == list(FEATURE_ORDER)
            assert "created_at" in manifest
            assert "created_at_utc" in manifest
            assert "sha256" in manifest
            assert "model.onnx" in manifest["sha256"]

    def test_manifest_validates_with_load_artifact(self) -> None:
        """Test that built artifact passes full validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            model_path = tmpdir_path / "model.onnx"
            model_path.write_bytes(b"model bytes")

            output_dir = tmpdir_path / "artifact"
            build_artifact(
                model_path=model_path,
                output_dir=output_dir,
                dataset_id="ci_test",
            )

            # Should load without error
            artifact = load_artifact(output_dir)
            assert artifact.manifest.schema_version == ARTIFACT_SCHEMA_VERSION
            assert artifact.manifest.dataset_id == "ci_test"
            assert artifact.manifest.feature_order == FEATURE_ORDER

    def test_git_sha_included_when_available(self) -> None:
        """Test that git_sha is included when in a git repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            model_path = tmpdir_path / "model.onnx"
            model_path.write_bytes(b"model")

            output_dir = tmpdir_path / "artifact"

            # Mock git to return known SHA
            mock_sha = "a" * 40
            with patch(
                "scripts.build_onnx_artifact.get_git_sha",
                return_value=mock_sha,
            ):
                build_artifact(
                    model_path=model_path,
                    output_dir=output_dir,
                    dataset_id="test",
                )

            with (output_dir / "manifest.json").open() as f:
                manifest = json.load(f)

            assert manifest.get("git_sha") == mock_sha

    def test_git_sha_omitted_when_unavailable(self) -> None:
        """Test that git_sha is omitted when not in git repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            model_path = tmpdir_path / "model.onnx"
            model_path.write_bytes(b"model")

            output_dir = tmpdir_path / "artifact"

            with patch(
                "scripts.build_onnx_artifact.get_git_sha",
                return_value=None,
            ):
                build_artifact(
                    model_path=model_path,
                    output_dir=output_dir,
                    dataset_id="test",
                )

            with (output_dir / "manifest.json").open() as f:
                manifest = json.load(f)

            assert "git_sha" not in manifest

    def test_raises_if_model_not_found(self) -> None:
        """Test error when model file doesn't exist."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(ValueError, match="Model file not found"),
        ):
            build_artifact(
                model_path=Path(tmpdir) / "nonexistent.onnx",
                output_dir=Path(tmpdir) / "artifact",
                dataset_id="test",
            )

    def test_raises_if_output_exists(self) -> None:
        """Test error when output directory already exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            model_path = tmpdir_path / "model.onnx"
            model_path.write_bytes(b"model")

            output_dir = tmpdir_path / "artifact"
            output_dir.mkdir()  # Pre-create

            with pytest.raises(ValueError, match="already exists"):
                build_artifact(
                    model_path=model_path,
                    output_dir=output_dir,
                    dataset_id="test",
                )

    def test_notes_optional(self) -> None:
        """Test that notes field is optional."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            model_path = tmpdir_path / "model.onnx"
            model_path.write_bytes(b"model")

            output_dir = tmpdir_path / "artifact"
            build_artifact(
                model_path=model_path,
                output_dir=output_dir,
                dataset_id="test",
                notes=None,
            )

            with (output_dir / "manifest.json").open() as f:
                manifest = json.load(f)

            assert "notes" not in manifest

    def test_created_at_utc_format(self) -> None:
        """Test that created_at_utc is in UTC ISO8601 format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            model_path = tmpdir_path / "model.onnx"
            model_path.write_bytes(b"model")

            output_dir = tmpdir_path / "artifact"
            build_artifact(
                model_path=model_path,
                output_dir=output_dir,
                dataset_id="test",
            )

            with (output_dir / "manifest.json").open() as f:
                manifest = json.load(f)

            # Should end with Z (UTC)
            assert manifest["created_at_utc"].endswith("Z")
            # Should be valid ISO format (YYYY-MM-DDTHH:MM:SSZ)
            assert "T" in manifest["created_at_utc"]
