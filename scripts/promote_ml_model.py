#!/usr/bin/env python3
"""Promote ML model between stages with audit trail.

M8-03c-3: Safe model promotion with history[] tracking and fail-closed guards.

Usage:
    # Promote to SHADOW (validation/testing)
    python -m scripts.promote_ml_model \\
        --model regime_classifier \\
        --stage shadow \\
        --artifact-dir ml/artifacts/regime_v2 \\
        --artifact-id regime_v2_candidate \\
        --dataset-id market_data_2026_q1 \\
        --notes "New candidate model for validation"

    # Promote to ACTIVE (production) - requires git_sha
    python -m scripts.promote_ml_model \\
        --model regime_classifier \\
        --stage active \\
        --artifact-dir ml/artifacts/regime_v2 \\
        --artifact-id regime_v2_prod \\
        --dataset-id market_data_2026_q1 \\
        --git-sha <40-char-hex> \\
        --notes "Promoted after 7 days SHADOW validation"

    # Dry-run mode (preview changes without writing)
    python -m scripts.promote_ml_model --dry-run --model ... --stage ...
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grinder.ml.onnx.artifact import load_artifact
from grinder.ml.onnx.registry import ModelRegistry, RegistryError, Stage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_actor() -> str | None:
    """Get actor for promotion audit trail.

    Priority:
        1. ML_PROMOTION_ACTOR env var
        2. git config user.email
        3. None

    Returns:
        Actor identifier (email) or None
    """
    # Try env var first
    actor = os.environ.get("ML_PROMOTION_ACTOR")
    if actor:
        return actor

    # Try git config
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            email = result.stdout.strip()
            if email:
                return email
    except FileNotFoundError:
        pass

    return None


def promote_model(  # noqa: PLR0912, PLR0915
    registry_path: Path,
    model_name: str,
    stage: Stage,
    artifact_dir: str,
    artifact_id: str,
    dataset_id: str | None,
    git_sha: str | None,
    notes: str | None,
    reason: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Promote model to target stage with audit trail.

    Args:
        registry_path: Path to registry JSON file
        model_name: Model name to promote
        stage: Target stage (shadow/staging/active)
        artifact_dir: Relative artifact directory path
        artifact_id: Artifact identifier
        dataset_id: Training dataset identifier (required for ACTIVE)
        git_sha: Git commit SHA (required for ACTIVE, 40-char hex)
        notes: Optional human-readable notes
        reason: Optional promotion reason
        dry_run: If True, preview changes without writing

    Returns:
        Updated registry data dict

    Raises:
        RegistryError: If validation fails or promotion is unsafe
        FileNotFoundError: If registry or artifact not found
    """
    # Load existing registry
    logger.info("Loading registry: %s", registry_path)
    registry = ModelRegistry.load(registry_path)

    # Validate model exists
    if model_name not in registry.models:
        raise RegistryError(
            f"Model {model_name!r} not found in registry. "
            f"Available: {sorted(registry.models.keys())}"
        )

    # Validate path safety (no traversal, no absolute)
    if Path(artifact_dir).is_absolute():
        raise RegistryError(f"Absolute paths not allowed: {artifact_dir!r}")

    if ".." in artifact_dir.split("/"):
        raise RegistryError(f"Path traversal not allowed: {artifact_dir!r}")

    # ACTIVE mode: strict validation (fail-closed)
    if stage == Stage.ACTIVE:
        if git_sha is None:
            raise RegistryError("ACTIVE promotion requires --git-sha (40-char hex commit SHA)")

        if not isinstance(git_sha, str) or len(git_sha) != 40:
            raise RegistryError("Invalid git_sha for ACTIVE: must be 40-char hex string")

        if not re.match(r"^[0-9a-f]{40}$", git_sha):
            raise RegistryError(
                f"Invalid git_sha format for ACTIVE: {git_sha!r} (must be lowercase hex)"
            )

        if dataset_id is None or not dataset_id:
            raise RegistryError(
                "ACTIVE promotion requires --dataset-id (non-empty training dataset identifier)"
            )

    # Verify artifact exists and is valid
    base_dir = registry_path.parent
    artifact_path = (base_dir / artifact_dir).resolve()

    # Verify containment
    try:
        artifact_path.relative_to(base_dir.resolve())
    except ValueError:
        raise RegistryError(f"Artifact path escapes base directory: {artifact_dir!r}") from None

    if not artifact_path.exists():
        raise RegistryError(f"Artifact directory not found: {artifact_path}")

    if not artifact_path.is_dir():
        raise RegistryError(f"Artifact path is not a directory: {artifact_path}")

    # Verify artifact integrity
    try:
        artifact = load_artifact(artifact_path)
        logger.info(
            "Artifact verified: %d files checked",
            len(artifact.manifest.sha256),
        )
    except Exception as e:
        raise RegistryError(f"Artifact validation failed: {e}") from e

    # Get actor for audit trail
    actor = get_actor()
    if actor:
        logger.info("Actor: %s", actor)
    else:
        logger.warning(
            "Actor: null — set ML_PROMOTION_ACTOR env or git config user.email "
            "for production audit trail"
        )

    # Get current timestamp (UTC, Z suffix)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017
    promoted_at_utc = ts.replace("+00:00", "Z")

    # Load existing registry JSON for modification
    with registry_path.open() as f:
        registry_data: dict[str, Any] = json.load(f)

    # Create history event
    # from_stage is None: CLI sets target stage directly, no cross-stage tracking.
    # Cross-stage lineage (e.g., shadow→active) can be inferred from history sequence.
    history_event = {
        "ts_utc": promoted_at_utc,
        "from_stage": None,
        "to_stage": stage.value,
        "actor": actor,
        "source": "cli",
        "reason": reason,
        "notes": notes,
        "pointer": {
            "artifact_dir": artifact_dir,
            "artifact_id": artifact_id,
            "git_sha": git_sha,
            "dataset_id": dataset_id,
            "promoted_at_utc": promoted_at_utc,
            "notes": notes,
            "actor": actor,
            "source": "cli",
            "feature_order_hash": None,
        },
        "registry_git_sha": None,  # To be filled by CI or manual git SHA
    }

    # Update registry data
    registry_data["models"][model_name][stage.value] = {
        "artifact_dir": artifact_dir,
        "artifact_id": artifact_id,
        "git_sha": git_sha,
        "dataset_id": dataset_id,
        "promoted_at_utc": promoted_at_utc,
        "notes": notes,
        "actor": actor,
        "source": "cli",
        "feature_order_hash": None,
    }

    # Append to history (newest first recommended, but we'll prepend)
    if "history" not in registry_data["models"][model_name]:
        registry_data["models"][model_name]["history"] = []

    registry_data["models"][model_name]["history"].insert(0, history_event)

    # Enforce max 50 history entries
    if len(registry_data["models"][model_name]["history"]) > 50:
        registry_data["models"][model_name]["history"] = registry_data["models"][model_name][
            "history"
        ][:50]
        logger.warning("History truncated to 50 entries")

    # Update generated_at_utc
    registry_data["generated_at_utc"] = promoted_at_utc

    if dry_run:
        logger.info("=== DRY RUN MODE (no changes written) ===")
        logger.info("Target stage: %s", stage.value)
        logger.info("Artifact: %s (%s)", artifact_id, artifact_dir)
        logger.info("Dataset: %s", dataset_id or "null")
        logger.info("Git SHA: %s", git_sha or "null")
        logger.info("")
        logger.info("Updated registry preview:")
        print(json.dumps(registry_data, indent=2, ensure_ascii=False))
    else:
        # Write updated registry (deterministic JSON)
        with registry_path.open("w") as f:
            json.dump(registry_data, f, indent=2, ensure_ascii=False)
            f.write("\n")  # Trailing newline

        logger.info("✓ Promotion complete")
        logger.info("  Model: %s", model_name)
        logger.info("  Stage: %s", stage.value)
        logger.info("  Artifact: %s", artifact_id)
        logger.info("  Registry: %s", registry_path)

    return registry_data


def main() -> int:
    """Main entry point.

    Returns:
        0 if promotion succeeds, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Promote ML model between stages with audit trail")
    parser.add_argument(
        "--registry-path",
        type=Path,
        default=Path("ml/registry/models.json"),
        help="Path to registry JSON file (default: ml/registry/models.json)",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model name to promote",
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=["shadow", "staging", "active"],
        help="Target stage (shadow/staging/active)",
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        help="Relative artifact directory path (no .., no absolute paths)",
    )
    parser.add_argument(
        "--artifact-id",
        required=True,
        help="Artifact identifier (human-readable)",
    )
    parser.add_argument(
        "--dataset-id",
        default=None,
        help="Training dataset identifier (required for ACTIVE)",
    )
    parser.add_argument(
        "--git-sha",
        default=None,
        help="Git commit SHA - 40-char hex (required for ACTIVE)",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="Optional human-readable notes",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Optional promotion reason for audit trail",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to registry",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        stage = Stage(args.stage)
        promote_model(
            registry_path=args.registry_path,
            model_name=args.model,
            stage=stage,
            artifact_dir=args.artifact_dir,
            artifact_id=args.artifact_id,
            dataset_id=args.dataset_id,
            git_sha=args.git_sha,
            notes=args.notes,
            reason=args.reason,
            dry_run=args.dry_run,
        )
        return 0
    except (RegistryError, FileNotFoundError) as e:
        logger.error("Promotion failed: %s", e)
        return 1
    except Exception:
        logger.exception("Unexpected error during promotion")
        return 1


if __name__ == "__main__":
    sys.exit(main())
