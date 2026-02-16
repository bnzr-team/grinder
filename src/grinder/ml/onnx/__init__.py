"""ONNX model integration.

M8-02a: Artifact loading and validation.
M8-02b: Shadow mode inference.

This module provides:
- OnnxArtifactManifest: Manifest schema for ONNX artifacts
- OnnxArtifact: Loaded and validated artifact
- OnnxMlModel: ONNX model for regime prediction
- load_artifact(): Main entry point for loading artifacts
- ONNX_AVAILABLE: Whether onnxruntime is installed
- Error types for specific failure modes
"""

from __future__ import annotations

from .artifact import load_artifact, load_manifest, validate_checksums
from .features import FEATURE_ORDER, vectorize
from .model import OnnxMlModel, OnnxModelError
from .runtime import ONNX_AVAILABLE, OnnxRuntimeError, OnnxSession
from .types import (
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_SCHEMA_VERSIONS,
    OnnxArtifact,
    OnnxArtifactError,
    OnnxArtifactManifest,
    OnnxChecksumError,
    OnnxManifestError,
    OnnxPathError,
)

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ARTIFACT_SCHEMA_VERSIONS",
    "FEATURE_ORDER",
    "ONNX_AVAILABLE",
    "OnnxArtifact",
    "OnnxArtifactError",
    "OnnxArtifactManifest",
    "OnnxChecksumError",
    "OnnxManifestError",
    "OnnxMlModel",
    "OnnxModelError",
    "OnnxPathError",
    "OnnxRuntimeError",
    "OnnxSession",
    "load_artifact",
    "load_manifest",
    "validate_checksums",
    "vectorize",
]
