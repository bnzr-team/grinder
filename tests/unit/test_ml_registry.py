"""Unit tests for ML model registry.

M8-03c-1b: Registry loader and validation tests.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from grinder.ml.onnx.registry import (
    ModelRegistry,
    RegistryError,
    Stage,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_load_valid_registry(tmp_path: Path) -> None:
    """Load a valid registry."""
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
                            "git_sha": None,
                            "dataset_id": "dataset1",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    registry = ModelRegistry.load(registry_file)

    assert registry.schema_version == "v1"
    assert len(registry.models) == 1
    assert "test_model" in registry.models

    shadow = registry.get_stage_pointer("test_model", Stage.SHADOW)
    assert shadow is not None
    assert shadow.artifact_dir == "artifacts/shadow"
    assert shadow.artifact_id == "shadow_v1"
    assert shadow.git_sha is None
    assert shadow.dataset_id == "dataset1"

    active = registry.get_stage_pointer("test_model", Stage.ACTIVE)
    assert active is None


def test_invalid_schema_version(tmp_path: Path) -> None:
    """Reject invalid schema_version."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(json.dumps({"schema_version": "v2", "models": {}}))

    with pytest.raises(RegistryError, match="Invalid schema_version"):
        ModelRegistry.load(registry_file)


def test_missing_models(tmp_path: Path) -> None:
    """Reject registry without models."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(json.dumps({"schema_version": "v1"}))

    with pytest.raises(RegistryError, match="'models' must be non-empty dict"):
        ModelRegistry.load(registry_file)


def test_empty_models(tmp_path: Path) -> None:
    """Reject registry with empty models dict."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(json.dumps({"schema_version": "v1", "models": {}}))

    with pytest.raises(RegistryError, match="'models' must be non-empty dict"):
        ModelRegistry.load(registry_file)


def test_invalid_model_name_uppercase(tmp_path: Path) -> None:
    """Reject model_name with uppercase letters."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {"InvalidName": {"shadow": None, "active": None}},
            }
        )
    )

    with pytest.raises(RegistryError, match="Invalid model_name"):
        ModelRegistry.load(registry_file)


def test_invalid_model_name_special_chars(tmp_path: Path) -> None:
    """Reject model_name with special characters."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {"model-name": {"shadow": None, "active": None}},
            }
        )
    )

    with pytest.raises(RegistryError, match="Invalid model_name"):
        ModelRegistry.load(registry_file)


def test_path_traversal_blocked(tmp_path: Path) -> None:
    """Block path traversal attempts (..)."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "../../../etc/passwd",
                            "artifact_id": "malicious",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    with pytest.raises(RegistryError, match="Path traversal not allowed"):
        ModelRegistry.load(registry_file)


def test_absolute_path_blocked(tmp_path: Path) -> None:
    """Block absolute paths."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "/etc/passwd",
                            "artifact_id": "malicious",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    with pytest.raises(RegistryError, match="Absolute paths not allowed"):
        ModelRegistry.load(registry_file)


def test_missing_artifact_dir(tmp_path: Path) -> None:
    """Reject pointer without artifact_dir."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {"artifact_id": "shadow_v1"},
                        "active": None,
                    }
                },
            }
        )
    )

    with pytest.raises(RegistryError, match="Missing or invalid 'artifact_dir'"):
        ModelRegistry.load(registry_file)


def test_missing_artifact_id(tmp_path: Path) -> None:
    """Reject pointer without artifact_id."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {"artifact_dir": "artifacts/shadow"},
                        "active": None,
                    }
                },
            }
        )
    )

    with pytest.raises(RegistryError, match="Missing or invalid 'artifact_id'"):
        ModelRegistry.load(registry_file)


def test_invalid_git_sha_length(tmp_path: Path) -> None:
    """Reject git_sha with wrong length."""
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
                            "git_sha": "short",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    with pytest.raises(RegistryError, match="must be 40-char hex string"):
        ModelRegistry.load(registry_file)


def test_invalid_git_sha_format(tmp_path: Path) -> None:
    """Reject git_sha with non-hex characters."""
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
                            "git_sha": "g" * 40,  # 'g' is not hex
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    with pytest.raises(RegistryError, match="Invalid git_sha format"):
        ModelRegistry.load(registry_file)


def test_valid_git_sha(tmp_path: Path) -> None:
    """Accept valid git_sha."""
    valid_sha = "a" * 40
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
                            "git_sha": valid_sha,
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    registry = ModelRegistry.load(registry_file)
    shadow = registry.get_stage_pointer("test_model", Stage.SHADOW)
    assert shadow is not None
    assert shadow.git_sha == valid_sha


def test_model_not_found(tmp_path: Path) -> None:
    """Raise error when model_name not in registry."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {"test_model": {"shadow": None, "active": None}},
            }
        )
    )

    registry = ModelRegistry.load(registry_file)

    with pytest.raises(RegistryError, match="Model 'unknown' not found"):
        registry.get_stage_pointer("unknown", Stage.SHADOW)


def test_resolve_artifact_dir(tmp_path: Path) -> None:
    """Resolve artifact_dir from pointer."""
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

    registry = ModelRegistry.load(registry_file)
    shadow = registry.get_stage_pointer("test_model", Stage.SHADOW)
    assert shadow is not None

    # Resolve with default base_dir (registry parent)
    resolved = registry.resolve_artifact_dir(shadow)
    assert resolved == tmp_path / "artifacts/shadow"

    # Resolve with custom base_dir
    custom_base = tmp_path / "custom"
    custom_base.mkdir()
    resolved_custom = registry.resolve_artifact_dir(shadow, custom_base)
    assert resolved_custom == custom_base / "artifacts/shadow"


def test_resolve_artifact_dir_escape_attempt(tmp_path: Path) -> None:
    """Block artifact_dir that escapes base after resolution."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "models": {
                    "test_model": {
                        "shadow": {
                            "artifact_dir": "artifacts/../../etc",
                            "artifact_id": "malicious",
                        },
                        "active": None,
                    }
                },
            }
        )
    )

    # This should be caught at load time (path traversal check)
    with pytest.raises(RegistryError, match="Path traversal not allowed"):
        ModelRegistry.load(registry_file)


def test_file_not_found(tmp_path: Path) -> None:
    """Raise FileNotFoundError if registry file doesn't exist."""
    nonexistent = tmp_path / "nonexistent.json"

    with pytest.raises(FileNotFoundError):
        ModelRegistry.load(nonexistent)


def test_invalid_json(tmp_path: Path) -> None:
    """Reject malformed JSON."""
    registry_file = tmp_path / "models.json"
    registry_file.write_text("{ invalid json }")

    with pytest.raises(RegistryError, match="Invalid JSON"):
        ModelRegistry.load(registry_file)
