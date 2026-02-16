"""ML Model Registry loader and validation.

M8-03c-1b: Git-based SSOT for model artifacts and promotion pointers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# Valid model name pattern (lowercase alphanumeric + underscore)
MODEL_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


class Stage(str, Enum):
    """Model deployment stage."""

    SHADOW = "shadow"
    ACTIVE = "active"


class RegistryError(Exception):
    """Registry validation or loading error."""

    pass


@dataclass(frozen=True)
class ModelPointer:
    """Pointer to a model artifact for a specific stage.

    Args:
        artifact_dir: Relative path to artifact directory (validated for safety)
        artifact_id: Human-readable artifact identifier
        git_sha: Git commit SHA (40-char hex) or None
        dataset_id: Training dataset identifier or None
    """

    artifact_dir: str
    artifact_id: str
    git_sha: str | None
    dataset_id: str | None


@dataclass(frozen=True)
class ModelRegistry:
    """ML Model Registry (SSOT).

    Immutable registry loaded from JSON file. Provides safe stage pointer
    resolution with strict validation.

    Args:
        registry_path: Path to registry JSON file
        schema_version: Registry schema version (must be "v1")
        models: Dict of model_name -> stage pointers
    """

    registry_path: Path
    schema_version: str
    models: dict[str, dict[str, ModelPointer | None]]

    @classmethod
    def load(cls, path: Path | str) -> ModelRegistry:  # noqa: PLR0912
        """Load and validate registry from JSON file.

        Args:
            path: Path to registry JSON file

        Returns:
            Validated ModelRegistry instance

        Raises:
            RegistryError: If registry is invalid or malformed
            FileNotFoundError: If registry file does not exist
        """
        registry_path = Path(path).resolve()

        if not registry_path.exists():
            raise FileNotFoundError(f"Registry file not found: {registry_path}")

        try:
            with registry_path.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise RegistryError(f"Invalid JSON in registry: {e}") from e

        # Validate schema_version
        schema_version = data.get("schema_version")
        if schema_version != "v1":
            raise RegistryError(f"Invalid schema_version: {schema_version!r} (expected 'v1')")

        # Validate models
        models_data = data.get("models")
        if not isinstance(models_data, dict) or not models_data:
            raise RegistryError("Registry 'models' must be non-empty dict")

        models: dict[str, dict[str, ModelPointer | None]] = {}

        for model_name, model_config in models_data.items():
            # Validate model_name pattern
            if not MODEL_NAME_PATTERN.match(model_name):
                raise RegistryError(
                    f"Invalid model_name: {model_name!r} (must match {MODEL_NAME_PATTERN.pattern})"
                )

            if not isinstance(model_config, dict):
                raise RegistryError(f"Model config for {model_name!r} must be dict")

            stage_pointers: dict[str, ModelPointer | None] = {}

            for stage in Stage:
                pointer_data = model_config.get(stage.value)

                if pointer_data is None:
                    stage_pointers[stage.value] = None
                    continue

                if not isinstance(pointer_data, dict):
                    raise RegistryError(
                        f"Pointer for {model_name}/{stage.value} must be dict or null"
                    )

                # Validate required fields
                artifact_dir = pointer_data.get("artifact_dir")
                artifact_id = pointer_data.get("artifact_id")

                if not artifact_dir or not isinstance(artifact_dir, str):
                    raise RegistryError(
                        f"Missing or invalid 'artifact_dir' for {model_name}/{stage.value}"
                    )

                if not artifact_id or not isinstance(artifact_id, str):
                    raise RegistryError(
                        f"Missing or invalid 'artifact_id' for {model_name}/{stage.value}"
                    )

                # Validate artifact_dir path safety (no traversal, no absolute)
                _validate_path_safety(artifact_dir, model_name, stage.value)

                # Optional fields
                git_sha = pointer_data.get("git_sha")
                dataset_id = pointer_data.get("dataset_id")

                # Validate git_sha format if present
                if git_sha is not None:
                    if not isinstance(git_sha, str) or len(git_sha) != 40:
                        raise RegistryError(
                            f"Invalid git_sha for {model_name}/{stage.value}: "
                            f"must be 40-char hex string or null"
                        )
                    if not re.match(r"^[0-9a-f]{40}$", git_sha):
                        raise RegistryError(
                            f"Invalid git_sha format for {model_name}/{stage.value}: {git_sha!r}"
                        )

                stage_pointers[stage.value] = ModelPointer(
                    artifact_dir=artifact_dir,
                    artifact_id=artifact_id,
                    git_sha=git_sha,
                    dataset_id=dataset_id,
                )

            models[model_name] = stage_pointers

        logger.info(
            "Registry loaded: path=%s models=%d",
            registry_path,
            len(models),
        )

        return cls(
            registry_path=registry_path,
            schema_version=schema_version,
            models=models,
        )

    def get_stage_pointer(self, model_name: str, stage: Stage) -> ModelPointer | None:
        """Get stage pointer for a model.

        Args:
            model_name: Model name (must exist in registry)
            stage: Deployment stage (shadow/active)

        Returns:
            ModelPointer if stage has pointer, None if pointer is null

        Raises:
            RegistryError: If model_name not found in registry
        """
        if model_name not in self.models:
            raise RegistryError(
                f"Model {model_name!r} not found in registry. "
                f"Available: {sorted(self.models.keys())}"
            )

        pointer = self.models[model_name].get(stage.value)
        return pointer

    def resolve_artifact_dir(self, pointer: ModelPointer, base_dir: Path | None = None) -> Path:
        """Resolve artifact directory from pointer.

        Args:
            pointer: Model pointer with artifact_dir
            base_dir: Base directory to resolve relative paths (defaults to registry parent)

        Returns:
            Absolute path to artifact directory

        Raises:
            RegistryError: If resolved path escapes base_dir
        """
        if base_dir is None:
            base_dir = self.registry_path.parent

        base_resolved = base_dir.resolve()
        artifact_path = (base_resolved / pointer.artifact_dir).resolve()

        # Verify containment
        try:
            artifact_path.relative_to(base_resolved)
        except ValueError:
            raise RegistryError(
                f"Artifact path escapes base directory: {pointer.artifact_dir!r}"
            ) from None

        return artifact_path


def _validate_path_safety(path: str, model_name: str, stage: str) -> None:
    """Validate that path is safe (no traversal, no absolute).

    Args:
        path: Relative path from registry
        model_name: Model name (for error messages)
        stage: Stage name (for error messages)

    Raises:
        RegistryError: If path is unsafe
    """
    # Reject absolute paths
    if Path(path).is_absolute():
        raise RegistryError(f"Absolute paths not allowed in {model_name}/{stage}: {path!r}")

    # Reject path traversal (..)
    if ".." in path.split("/"):
        raise RegistryError(f"Path traversal not allowed in {model_name}/{stage}: {path!r}")


__all__ = [
    "ModelPointer",
    "ModelRegistry",
    "RegistryError",
    "Stage",
]
