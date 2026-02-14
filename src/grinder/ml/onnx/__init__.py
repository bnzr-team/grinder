"""ONNX model integration.

M8-02a: Artifact loading and validation (no inference).

This module provides:
- OnnxArtifactManifest: Manifest schema for ONNX artifacts
- OnnxArtifact: Loaded and validated artifact
- load_artifact(): Main entry point for loading artifacts
- Error types for specific failure modes
"""

from __future__ import annotations

from .artifact import load_artifact, load_manifest, validate_checksums
from .types import (
    ARTIFACT_SCHEMA_VERSION,
    OnnxArtifact,
    OnnxArtifactError,
    OnnxArtifactManifest,
    OnnxChecksumError,
    OnnxManifestError,
    OnnxPathError,
)

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "OnnxArtifact",
    "OnnxArtifactError",
    "OnnxArtifactManifest",
    "OnnxChecksumError",
    "OnnxManifestError",
    "OnnxPathError",
    "load_artifact",
    "load_manifest",
    "validate_checksums",
]
