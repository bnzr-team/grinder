"""Shared pytest fixtures for unit tests."""

from __future__ import annotations

import json
import shutil
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
    # Copy test artifact to tmp_path to avoid absolute path issues
    artifact_dst = tmp_path / "test_artifact"
    if TEST_ARTIFACT_DIR.exists():
        shutil.copytree(TEST_ARTIFACT_DIR, artifact_dst)
    else:
        # Create minimal dummy artifact for tests when real artifact doesn't exist
        artifact_dst.mkdir()
        (artifact_dst / "manifest.json").write_text('{"model_sha256":"test"}')

    # Create registry file pointing to local artifact
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "test_artifact",
                            "artifact_id": "test_active_v1",
                        },
                        "active": {
                            "artifact_dir": "test_artifact",
                            "artifact_id": "test_active_v1",
                            "git_sha": "1234567890abcdef1234567890abcdef12345678",
                            "dataset_id": "test_dataset_v1",
                            "promoted_at_utc": "2026-01-01T00:00:00Z",
                        },
                    }
                },
            }
        )
    )

    yield str(registry_file), "test_model", "active"
