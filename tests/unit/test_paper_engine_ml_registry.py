"""Unit tests for PaperEngine ML registry wiring.

M8-03c-2: Tests for registry resolution and fail-closed guards.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from grinder.paper.engine import PaperEngine

if TYPE_CHECKING:
    from pathlib import Path


def test_registry_resolution_shadow_mode(tmp_path: Path) -> None:
    """Resolve artifact from registry in SHADOW mode."""
    # Create registry
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "artifacts/shadow",
                            "artifact_id": "shadow_v1",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    # Create artifact directory (minimal stub)
    artifact_dir = tmp_path / "artifacts" / "shadow"
    artifact_dir.mkdir(parents=True)

    # Create PaperEngine with registry config
    engine = PaperEngine(
        ml_shadow_mode=True,
        ml_infer_enabled=True,
        ml_registry_path=str(registry_file),
        ml_model_name="test_model",
        ml_stage="shadow",
    )

    # Verify artifact dir was resolved
    assert engine._onnx_artifact_dir == str(artifact_dir)
    assert engine._onnx_artifact_source == "registry"


def test_legacy_fallback_shadow_mode(tmp_path: Path) -> None:
    """Legacy onnx_artifact_dir fallback in SHADOW mode."""
    artifact_dir = tmp_path / "artifacts" / "legacy"
    artifact_dir.mkdir(parents=True)

    # Create PaperEngine with legacy config (no registry)
    engine = PaperEngine(
        ml_shadow_mode=True,
        ml_infer_enabled=True,
        onnx_artifact_dir=str(artifact_dir),
    )

    # Verify legacy artifact dir was used
    assert engine._onnx_artifact_dir == str(artifact_dir)
    assert engine._onnx_artifact_source == "legacy"


def test_no_artifact_configured() -> None:
    """No artifact configured (ML disabled)."""
    # Create PaperEngine with no ML config
    engine = PaperEngine()

    # Verify no artifact dir
    assert engine._onnx_artifact_dir is None
    assert engine._onnx_artifact_source == "none"


def test_active_mode_requires_registry(tmp_path: Path) -> None:
    """ACTIVE mode blocks legacy fallback (fail-closed)."""
    artifact_dir = tmp_path / "artifacts" / "legacy"
    artifact_dir.mkdir(parents=True)

    # Attempt ACTIVE mode with legacy config (should fail)
    with pytest.raises(
        ValueError,
        match=r"ACTIVE mode requires ML registry.*Legacy onnx_artifact_dir is not allowed",
    ):
        PaperEngine(
            ml_active_enabled=True,
            ml_infer_enabled=True,
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            onnx_artifact_dir=str(artifact_dir),
        )


def test_registry_resolution_active_mode(tmp_path: Path) -> None:
    """Resolve artifact from registry in ACTIVE mode."""
    # Create registry
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": None,
                        "active": {
                            "artifact_dir": "artifacts/active",
                            "artifact_id": "active_v1",
                        },
                    }
                },
            }
        )
    )

    # Create artifact directory
    artifact_dir = tmp_path / "artifacts" / "active"
    artifact_dir.mkdir(parents=True)

    # Create PaperEngine with ACTIVE + registry
    engine = PaperEngine(
        ml_active_enabled=True,
        ml_infer_enabled=True,
        ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
        ml_registry_path=str(registry_file),
        ml_model_name="test_model",
        ml_stage="active",
    )

    # Verify artifact dir was resolved from registry
    assert engine._onnx_artifact_dir == str(artifact_dir)
    assert engine._onnx_artifact_source == "registry"


def test_registry_resolution_failure() -> None:
    """Registry resolution failure (fail-closed)."""
    # Attempt registry resolution with nonexistent file
    with pytest.raises(ValueError, match="Failed to resolve artifact from registry"):
        PaperEngine(
            ml_shadow_mode=True,
            ml_infer_enabled=True,
            ml_registry_path="/nonexistent/models.json",
            ml_model_name="test_model",
            ml_stage="shadow",
        )


def test_registry_resolution_model_not_found(tmp_path: Path) -> None:
    """Registry resolution failure: model not found."""
    # Create registry without target model
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "other_model": {
                        "shadow": {
                            "artifact_dir": "artifacts/shadow",
                            "artifact_id": "shadow_v1",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    # Attempt to resolve non-existent model
    with pytest.raises(ValueError, match="Failed to resolve artifact from registry"):
        PaperEngine(
            ml_shadow_mode=True,
            ml_infer_enabled=True,
            ml_registry_path=str(registry_file),
            ml_model_name="nonexistent_model",
            ml_stage="shadow",
        )


def test_registry_resolution_stage_not_configured(tmp_path: Path) -> None:
    """Registry resolution failure: stage pointer is null."""
    # Create registry with null shadow pointer
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": None,  # Not configured
                        "active": None,
                    }
                },
            }
        )
    )

    # Attempt to resolve null stage
    with pytest.raises(
        ValueError,
        match=r"Failed to resolve artifact from registry.*No shadow stage pointer",
    ):
        PaperEngine(
            ml_shadow_mode=True,
            ml_infer_enabled=True,
            ml_registry_path=str(registry_file),
            ml_model_name="test_model",
            ml_stage="shadow",
        )


def test_stage_default_to_shadow(tmp_path: Path) -> None:
    """Default ml_stage is 'shadow'."""
    # Create registry
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "artifacts/shadow",
                            "artifact_id": "shadow_v1",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    # Create artifact directory
    artifact_dir = tmp_path / "artifacts" / "shadow"
    artifact_dir.mkdir(parents=True)

    # Create PaperEngine without specifying ml_stage (should default to "shadow")
    engine = PaperEngine(
        ml_shadow_mode=True,
        ml_infer_enabled=True,
        ml_registry_path=str(registry_file),
        ml_model_name="test_model",
        # ml_stage not specified - should default to "shadow"
    )

    # Verify shadow artifact was resolved
    assert engine._onnx_artifact_dir == str(artifact_dir)
    assert engine._ml_stage == "shadow"
