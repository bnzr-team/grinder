"""ONNX artifact type definitions.

M8-02a: Artifact manifest and validation types (no inference).
M8-03a: Extended manifest v1.1 with git_sha, dataset_id, feature_order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 (used at runtime in dataclass)
from typing import Any


class OnnxArtifactError(Exception):
    """Base error for ONNX artifact operations."""

    pass


class OnnxManifestError(OnnxArtifactError):
    """Error in manifest structure or validation."""

    pass


class OnnxChecksumError(OnnxArtifactError):
    """SHA256 checksum mismatch."""

    pass


class OnnxPathError(OnnxArtifactError):
    """Invalid or unsafe path in manifest."""

    pass


# Supported schema versions (v1 = legacy, v1.1 = extended with traceability)
ARTIFACT_SCHEMA_VERSIONS = ("v1", "v1.1")
# Default schema version for new artifacts
ARTIFACT_SCHEMA_VERSION = "v1.1"


@dataclass(frozen=True)
class OnnxArtifactManifest:
    """ONNX artifact manifest (manifest.json).

    Defines the structure and integrity checksums for an ONNX model artifact.

    Fields (required):
        schema_version: Must be "v1" or "v1.1"
        model_file: Relative path to the ONNX model file (e.g., "model.onnx")
        sha256: Map of relative file paths to their SHA256 hex digests
        created_at: ISO 8601 timestamp when artifact was created (legacy, use created_at_utc)

    Fields (optional, v1.1):
        created_at_utc: ISO 8601 UTC timestamp (e.g., "2026-02-14T19:10:00Z")
        git_sha: Git commit SHA (40 hex chars) or None if unavailable
        dataset_id: Identifier for the training dataset
        feature_order: List of feature names in expected order
        notes: Optional human-readable notes
    """

    schema_version: str
    model_file: str
    sha256: dict[str, str]
    created_at: str
    # v1.1 optional fields
    created_at_utc: str | None = None
    git_sha: str | None = None
    dataset_id: str | None = None
    feature_order: tuple[str, ...] | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        """Validate manifest on construction."""
        self._validate()

    def _validate(self) -> None:
        """Validate manifest invariants.

        Raises:
            OnnxManifestError: If validation fails.
        """
        # Check schema version (accept v1 and v1.1)
        if self.schema_version not in ARTIFACT_SCHEMA_VERSIONS:
            raise OnnxManifestError(
                f"Unsupported schema_version: {self.schema_version!r}, "
                f"expected one of {ARTIFACT_SCHEMA_VERSIONS}"
            )

        # Check sha256 map is not empty
        if not self.sha256:
            raise OnnxManifestError("sha256 map cannot be empty")

        # Check model_file is in sha256 map
        if self.model_file not in self.sha256:
            raise OnnxManifestError(f"model_file {self.model_file!r} not found in sha256 map")

        # Check all sha256 values are valid hex strings (64 chars)
        for path, digest in self.sha256.items():
            if not isinstance(digest, str) or len(digest) != 64:
                raise OnnxManifestError(f"Invalid SHA256 for {path!r}: expected 64-char hex string")
            try:
                int(digest, 16)
            except ValueError:
                raise OnnxManifestError(
                    f"Invalid SHA256 for {path!r}: not a valid hex string"
                ) from None

        # Validate git_sha format if present
        if self.git_sha is not None:
            if not isinstance(self.git_sha, str) or len(self.git_sha) != 40:
                raise OnnxManifestError(
                    f"Invalid git_sha: expected 40-char hex string, got {self.git_sha!r}"
                )
            try:
                int(self.git_sha, 16)
            except ValueError:
                raise OnnxManifestError("Invalid git_sha: not a valid hex string") from None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OnnxArtifactManifest:
        """Create manifest from dict (parsed JSON).

        Args:
            d: Dict with manifest fields.

        Returns:
            OnnxArtifactManifest instance.

        Raises:
            OnnxManifestError: If required fields are missing.
        """
        required = ["schema_version", "model_file", "sha256", "created_at"]
        missing = [k for k in required if k not in d]
        if missing:
            raise OnnxManifestError(f"Missing required fields: {missing}")

        # Convert feature_order list to tuple if present
        feature_order = d.get("feature_order")
        if feature_order is not None:
            feature_order = tuple(feature_order)

        return cls(
            schema_version=d["schema_version"],
            model_file=d["model_file"],
            sha256=dict(d["sha256"]),
            created_at=d["created_at"],
            created_at_utc=d.get("created_at_utc"),
            git_sha=d.get("git_sha"),
            dataset_id=d.get("dataset_id"),
            feature_order=feature_order,
            notes=d.get("notes"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to dict for JSON serialization.

        Returns:
            Dict representation of manifest.
        """
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "model_file": self.model_file,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }
        # Add optional fields if present
        if self.created_at_utc is not None:
            result["created_at_utc"] = self.created_at_utc
        if self.git_sha is not None:
            result["git_sha"] = self.git_sha
        if self.dataset_id is not None:
            result["dataset_id"] = self.dataset_id
        if self.feature_order is not None:
            result["feature_order"] = list(self.feature_order)
        if self.notes is not None:
            result["notes"] = self.notes
        return result


@dataclass
class OnnxArtifact:
    """Loaded and validated ONNX artifact.

    Represents a fully validated artifact directory with manifest and model file.

    Fields:
        root: Path to the artifact directory
        manifest: Validated manifest
        model_path: Absolute path to the model file
    """

    root: Path
    manifest: OnnxArtifactManifest
    model_path: Path

    def __post_init__(self) -> None:
        """Ensure paths are resolved."""
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "model_path", self.model_path.resolve())


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ARTIFACT_SCHEMA_VERSIONS",
    "OnnxArtifact",
    "OnnxArtifactError",
    "OnnxArtifactManifest",
    "OnnxChecksumError",
    "OnnxManifestError",
    "OnnxPathError",
]
