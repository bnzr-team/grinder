"""ONNX artifact loader and validation.

M8-02a: Load and validate ONNX artifacts with SHA256 integrity checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .types import (
    OnnxArtifact,
    OnnxArtifactManifest,
    OnnxChecksumError,
    OnnxManifestError,
    OnnxPathError,
)

logger = logging.getLogger(__name__)

# Chunk size for SHA256 calculation (4MB)
SHA256_CHUNK_SIZE = 4 * 1024 * 1024


def _compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of a file in chunks.

    Args:
        file_path: Path to the file.

    Returns:
        Lowercase hex digest (64 characters).
    """
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(SHA256_CHUNK_SIZE):
            sha256.update(chunk)
    return sha256.hexdigest().lower()


def _validate_path_safety(root: Path, relative_path: str) -> Path:
    """Validate that a relative path is safe (no traversal, no absolute).

    Args:
        root: Root directory (resolved).
        relative_path: Relative path from manifest.

    Returns:
        Resolved absolute path.

    Raises:
        OnnxPathError: If path is unsafe.
    """
    # Reject absolute paths
    if Path(relative_path).is_absolute():
        raise OnnxPathError(f"Absolute paths not allowed: {relative_path!r}")

    # Reject path traversal
    if ".." in relative_path.split("/"):
        raise OnnxPathError(f"Path traversal not allowed: {relative_path!r}")

    # Resolve and verify containment
    resolved = (root / relative_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise OnnxPathError(f"Path escapes artifact root: {relative_path!r}") from None

    return resolved


def load_manifest(artifact_dir: Path) -> OnnxArtifactManifest:
    """Load and validate manifest.json from artifact directory.

    Args:
        artifact_dir: Path to the artifact directory.

    Returns:
        Validated OnnxArtifactManifest.

    Raises:
        OnnxManifestError: If manifest is missing or invalid.
    """
    manifest_path = artifact_dir / "manifest.json"
    if not manifest_path.exists():
        raise OnnxManifestError(f"manifest.json not found in {artifact_dir}")

    try:
        with manifest_path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise OnnxManifestError(f"Invalid JSON in manifest.json: {e}") from e

    return OnnxArtifactManifest.from_dict(data)


def validate_checksums(artifact_dir: Path, manifest: OnnxArtifactManifest) -> None:
    """Validate SHA256 checksums for all files in manifest.

    Args:
        artifact_dir: Path to the artifact directory (resolved).
        manifest: Validated manifest.

    Raises:
        OnnxPathError: If any path is unsafe.
        OnnxChecksumError: If any checksum mismatches.
        FileNotFoundError: If any file is missing.
    """
    root = artifact_dir.resolve()

    for relative_path, expected_sha256 in manifest.sha256.items():
        # Validate path safety
        file_path = _validate_path_safety(root, relative_path)

        # Check file exists
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")

        # Compute and compare checksum
        actual_sha256 = _compute_sha256(file_path)
        expected_lower = expected_sha256.lower()

        if actual_sha256 != expected_lower:
            raise OnnxChecksumError(
                f"SHA256 mismatch for {relative_path}: "
                f"expected {expected_lower}, got {actual_sha256}"
            )

        logger.debug("Checksum OK: %s", relative_path)


def load_artifact(artifact_dir: Path | str) -> OnnxArtifact:
    """Load and fully validate an ONNX artifact.

    This is the main entry point for loading artifacts. It:
    1. Loads and validates manifest.json
    2. Validates all paths are safe
    3. Verifies SHA256 checksums for all files

    Args:
        artifact_dir: Path to the artifact directory.

    Returns:
        Fully validated OnnxArtifact.

    Raises:
        OnnxManifestError: If manifest is invalid.
        OnnxPathError: If any path is unsafe.
        OnnxChecksumError: If any checksum mismatches.
        FileNotFoundError: If any file is missing.
    """
    artifact_dir = Path(artifact_dir).resolve()

    if not artifact_dir.is_dir():
        raise OnnxManifestError(f"Artifact directory does not exist: {artifact_dir}")

    logger.info("Loading ONNX artifact from %s", artifact_dir)

    # Load and validate manifest
    manifest = load_manifest(artifact_dir)
    logger.debug(
        "Manifest loaded: schema=%s, model=%s", manifest.schema_version, manifest.model_file
    )

    # Validate all checksums
    validate_checksums(artifact_dir, manifest)
    logger.info("All checksums validated (%d files)", len(manifest.sha256))

    # Build artifact
    model_path = _validate_path_safety(artifact_dir, manifest.model_file)

    return OnnxArtifact(
        root=artifact_dir,
        manifest=manifest,
        model_path=model_path,
    )


__all__ = [
    "load_artifact",
    "load_manifest",
    "validate_checksums",
]
