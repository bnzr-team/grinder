"""ONNX artifact type definitions.

M8-02a: Artifact manifest and validation types (no inference).
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


# Supported schema version
ARTIFACT_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class OnnxArtifactManifest:
    """ONNX artifact manifest (manifest.json).

    Defines the structure and integrity checksums for an ONNX model artifact.

    Fields:
        schema_version: Must be "v1"
        model_file: Relative path to the ONNX model file (e.g., "model.onnx")
        sha256: Map of relative file paths to their SHA256 hex digests
        created_at: ISO 8601 timestamp when artifact was created
        notes: Optional human-readable notes
    """

    schema_version: str
    model_file: str
    sha256: dict[str, str]
    created_at: str
    notes: str | None = None

    def __post_init__(self) -> None:
        """Validate manifest on construction."""
        self._validate()

    def _validate(self) -> None:
        """Validate manifest invariants.

        Raises:
            OnnxManifestError: If validation fails.
        """
        # Check schema version
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise OnnxManifestError(
                f"Unsupported schema_version: {self.schema_version!r}, "
                f"expected {ARTIFACT_SCHEMA_VERSION!r}"
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

        return cls(
            schema_version=d["schema_version"],
            model_file=d["model_file"],
            sha256=dict(d["sha256"]),
            created_at=d["created_at"],
            notes=d.get("notes"),
        )


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
    "OnnxArtifact",
    "OnnxArtifactError",
    "OnnxArtifactManifest",
    "OnnxChecksumError",
    "OnnxManifestError",
    "OnnxPathError",
]
