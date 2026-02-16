"""Shared pytest fixtures for unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

# Test artifact directory (used by ML tests)
TEST_ARTIFACT_DIR = Path(__file__).parent.parent / "testdata" / "onnx_artifacts" / "tiny_regime"


@pytest.fixture
def ml_registry_for_active(tmp_path: Path) -> Generator[tuple[str, str, str], None, None]:
    """Create temporary ML registry for ACTIVE mode tests.

    Returns:
        Tuple of (registry_path, model_name, stage) for use in PaperEngine config.

    Usage:
        registry_path, model_name, stage = ml_registry_for_active
        engine = PaperEngine(
            ml_active_enabled=True,
            ml_infer_enabled=True,
            ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING",
            ml_registry_path=registry_path,
            ml_model_name=model_name,
            ml_stage=stage,
        )
    """
    # Compute relative path from tmp_path to TEST_ARTIFACT_DIR
    try:
        relative_artifact = TEST_ARTIFACT_DIR.relative_to(tmp_path)
    except ValueError:
        # Not relative, use absolute path
        relative_artifact = TEST_ARTIFACT_DIR

    # Create registry file
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": str(relative_artifact),
                            "artifact_id": "test_active_v1",
                        },
                        "active": {
                            "artifact_dir": str(relative_artifact),
                            "artifact_id": "test_active_v1",
                        },
                    }
                },
            }
        )
    )

    yield str(registry_file), "test_model", "active"
