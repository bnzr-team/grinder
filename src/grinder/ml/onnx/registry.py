"""ML Model Registry loader and validation.

M8-03c-1b: Git-based SSOT for model artifacts and promotion pointers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# Valid model name pattern (lowercase alphanumeric + underscore)
MODEL_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


class Stage(StrEnum):
    """Model deployment stage."""

    SHADOW = "shadow"
    STAGING = "staging"
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
        promoted_at_utc: ISO8601 timestamp (UTC, Z suffix) or None
        notes: Optional human-readable notes
        actor: Optional promotion actor (email or identifier)
        source: Optional promotion source (e.g., "cli", "ci")
        feature_order_hash: Optional deterministic feature ordering hash
    """

    artifact_dir: str
    artifact_id: str
    git_sha: str | None
    dataset_id: str | None
    promoted_at_utc: str | None = None
    notes: str | None = None
    actor: str | None = None
    source: str | None = None
    feature_order_hash: str | None = None


@dataclass(frozen=True)
class HistoryEvent:
    """Audit trail event for model promotion history.

    Args:
        ts_utc: ISO8601 timestamp (UTC, Z suffix) for the promotion event
        from_stage: Source stage (shadow/staging/active) or None for initial entry
        to_stage: Target stage (shadow/staging/active)
        actor: Promotion actor (email or identifier) or None
        source: Promotion source (e.g., "cli", "ci") or None
        reason: Human-readable promotion reason or None
        notes: Additional notes or None
        pointer: Snapshot of the pointer that was promoted
        registry_git_sha: Git SHA of registry commit (40-char hex) or None
    """

    ts_utc: str
    from_stage: str | None
    to_stage: str
    actor: str | None
    source: str | None
    reason: str | None
    notes: str | None
    pointer: ModelPointer
    registry_git_sha: str | None = None


@dataclass(frozen=True)
class ModelRegistry:
    """ML Model Registry (SSOT).

    Immutable registry loaded from JSON file. Provides safe stage pointer
    resolution with strict validation.

    Args:
        registry_path: Path to registry JSON file
        schema_version: Registry schema version (must be "v1")
        models: Dict of model_name -> stage pointers
        history: Dict of model_name -> history events (newest first recommended)
    """

    registry_path: Path
    schema_version: str
    models: dict[str, dict[str, ModelPointer | None]]
    history: dict[str, list[HistoryEvent]]

    @classmethod
    def load(cls, path: Path | str) -> ModelRegistry:  # noqa: PLR0912, PLR0915
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
        history: dict[str, list[HistoryEvent]] = {}

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
                promoted_at_utc = pointer_data.get("promoted_at_utc")
                notes = pointer_data.get("notes")
                actor = pointer_data.get("actor")
                source = pointer_data.get("source")
                feature_order_hash = pointer_data.get("feature_order_hash")

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

                # Validate promoted_at_utc format if present
                if promoted_at_utc is not None:
                    if not isinstance(promoted_at_utc, str):
                        raise RegistryError(
                            f"Invalid promoted_at_utc for {model_name}/{stage.value}: must be string or null"
                        )
                    if not promoted_at_utc.endswith("Z"):
                        raise RegistryError(
                            f"Invalid promoted_at_utc for {model_name}/{stage.value}: must end with 'Z' (UTC)"
                        )

                # ACTIVE mode: strict validation (fail-closed)
                if stage == Stage.ACTIVE:
                    if git_sha is None:
                        raise RegistryError(
                            f"ACTIVE pointer for {model_name} requires non-null git_sha"
                        )
                    if dataset_id is None or not dataset_id:
                        raise RegistryError(
                            f"ACTIVE pointer for {model_name} requires non-null, non-empty dataset_id"
                        )
                    if promoted_at_utc is None:
                        raise RegistryError(
                            f"ACTIVE pointer for {model_name} requires non-null promoted_at_utc"
                        )

                stage_pointers[stage.value] = ModelPointer(
                    artifact_dir=artifact_dir,
                    artifact_id=artifact_id,
                    git_sha=git_sha,
                    dataset_id=dataset_id,
                    promoted_at_utc=promoted_at_utc,
                    notes=notes,
                    actor=actor,
                    source=source,
                    feature_order_hash=feature_order_hash,
                )

            # Parse history (optional, default to empty list for backward compat)
            history_data = model_config.get("history", [])
            if not isinstance(history_data, list):
                raise RegistryError(f"History for {model_name} must be list")

            # Validate max history entries (50)
            if len(history_data) > 50:
                raise RegistryError(
                    f"History for {model_name} exceeds max 50 entries: {len(history_data)}"
                )

            history_events: list[HistoryEvent] = []
            for idx, event_data in enumerate(history_data):
                if not isinstance(event_data, dict):
                    raise RegistryError(f"History event {idx} for {model_name} must be dict")

                # Validate required fields
                ts_utc = event_data.get("ts_utc")
                to_stage = event_data.get("to_stage")
                pointer_data = event_data.get("pointer")

                if not ts_utc or not isinstance(ts_utc, str):
                    raise RegistryError(
                        f"History event {idx} for {model_name}: ts_utc required and must be string"
                    )

                if not ts_utc.endswith("Z"):
                    raise RegistryError(
                        f"History event {idx} for {model_name}: ts_utc must end with 'Z' (UTC)"
                    )

                if not to_stage or not isinstance(to_stage, str):
                    raise RegistryError(
                        f"History event {idx} for {model_name}: to_stage required and must be string"
                    )

                if to_stage not in [s.value for s in Stage]:
                    raise RegistryError(
                        f"History event {idx} for {model_name}: invalid to_stage {to_stage!r}"
                    )

                if not pointer_data or not isinstance(pointer_data, dict):
                    raise RegistryError(
                        f"History event {idx} for {model_name}: pointer required and must be dict"
                    )

                # Parse pointer from history event
                hist_artifact_dir = pointer_data.get("artifact_dir")
                hist_artifact_id = pointer_data.get("artifact_id")

                if not hist_artifact_dir or not isinstance(hist_artifact_dir, str):
                    raise RegistryError(
                        f"History event {idx} for {model_name}: pointer.artifact_dir required"
                    )

                if not hist_artifact_id or not isinstance(hist_artifact_id, str):
                    raise RegistryError(
                        f"History event {idx} for {model_name}: pointer.artifact_id required"
                    )

                # Validate path safety for history pointer
                _validate_path_safety(hist_artifact_dir, f"{model_name}/history[{idx}]", "pointer")

                # Parse optional pointer fields
                hist_git_sha = pointer_data.get("git_sha")
                hist_dataset_id = pointer_data.get("dataset_id")
                hist_promoted_at_utc = pointer_data.get("promoted_at_utc")
                hist_notes = pointer_data.get("notes")
                hist_actor = pointer_data.get("actor")
                hist_source = pointer_data.get("source")
                hist_feature_order_hash = pointer_data.get("feature_order_hash")

                # Validate git_sha format if present
                if hist_git_sha is not None:
                    if not isinstance(hist_git_sha, str) or len(hist_git_sha) != 40:
                        raise RegistryError(
                            f"History event {idx} for {model_name}: invalid pointer.git_sha"
                        )
                    if not re.match(r"^[0-9a-f]{40}$", hist_git_sha):
                        raise RegistryError(
                            f"History event {idx} for {model_name}: invalid pointer.git_sha format"
                        )

                hist_pointer = ModelPointer(
                    artifact_dir=hist_artifact_dir,
                    artifact_id=hist_artifact_id,
                    git_sha=hist_git_sha,
                    dataset_id=hist_dataset_id,
                    promoted_at_utc=hist_promoted_at_utc,
                    notes=hist_notes,
                    actor=hist_actor,
                    source=hist_source,
                    feature_order_hash=hist_feature_order_hash,
                )

                # Parse event fields
                from_stage = event_data.get("from_stage")
                actor = event_data.get("actor")
                source = event_data.get("source")
                reason = event_data.get("reason")
                notes = event_data.get("notes")
                registry_git_sha = event_data.get("registry_git_sha")

                # Validate registry_git_sha format if present
                if registry_git_sha is not None:
                    if not isinstance(registry_git_sha, str) or len(registry_git_sha) != 40:
                        raise RegistryError(
                            f"History event {idx} for {model_name}: invalid registry_git_sha"
                        )
                    if not re.match(r"^[0-9a-f]{40}$", registry_git_sha):
                        raise RegistryError(
                            f"History event {idx} for {model_name}: invalid registry_git_sha format"
                        )

                history_events.append(
                    HistoryEvent(
                        ts_utc=ts_utc,
                        from_stage=from_stage,
                        to_stage=to_stage,
                        actor=actor,
                        source=source,
                        reason=reason,
                        notes=notes,
                        pointer=hist_pointer,
                        registry_git_sha=registry_git_sha,
                    )
                )

            history[model_name] = history_events
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
            history=history,
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
    "HistoryEvent",
    "ModelPointer",
    "ModelRegistry",
    "RegistryError",
    "Stage",
]
