#!/usr/bin/env python3
"""Verify ML model registry and referenced artifacts.

M8-03c-1b: Validates registry schema, paths, and artifact integrity.

Usage:
    python -m scripts.verify_ml_registry --path ml/registry/models.json
    python -m scripts.verify_ml_registry --path ml/registry/models.json --base-dir /path/to/repo
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from grinder.ml.onnx.artifact import load_artifact
from grinder.ml.onnx.registry import ModelRegistry, RegistryError, Stage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def verify_registry(registry_path: Path, base_dir: Path | None = None) -> bool:  # noqa: PLR0912, PLR0915
    """Verify registry and all referenced artifacts.

    Args:
        registry_path: Path to registry JSON file
        base_dir: Base directory for resolving artifact paths (defaults to registry parent)

    Returns:
        True if all checks pass, False otherwise
    """
    try:
        # Load and validate registry
        logger.info("Loading registry: %s", registry_path)
        registry = ModelRegistry.load(registry_path)
        logger.info("Registry schema: %s", registry.schema_version)
        logger.info("Models: %d", len(registry.models))

        if base_dir is None:
            base_dir = registry_path.parent

        # Verify each model and stage
        all_valid = True
        for model_name in sorted(registry.models.keys()):
            logger.info("")
            logger.info("=== Model: %s ===", model_name)

            for stage in Stage:
                pointer = registry.get_stage_pointer(model_name, stage)

                if pointer is None:
                    logger.info("  %s: null (not configured)", stage.value.upper())
                    continue

                logger.info("  %s:", stage.value.upper())
                logger.info("    artifact_id: %s", pointer.artifact_id)
                logger.info("    artifact_dir: %s", pointer.artifact_dir)
                logger.info("    git_sha: %s", pointer.git_sha or "null")
                logger.info("    dataset_id: %s", pointer.dataset_id or "null")

                # Resolve artifact directory
                try:
                    artifact_dir = registry.resolve_artifact_dir(pointer, base_dir)
                    logger.info("    resolved_path: %s", artifact_dir)
                except RegistryError as e:
                    logger.error("    ERROR: %s", e)
                    all_valid = False
                    continue

                # Check if directory exists
                if not artifact_dir.exists():
                    logger.error(
                        "    ERROR: Artifact directory not found: %s",
                        artifact_dir,
                    )
                    all_valid = False
                    continue

                if not artifact_dir.is_dir():
                    logger.error(
                        "    ERROR: Artifact path is not a directory: %s",
                        artifact_dir,
                    )
                    all_valid = False
                    continue

                # Verify artifact integrity
                try:
                    artifact = load_artifact(artifact_dir)
                    logger.info(
                        "    ✓ Artifact valid: %d files verified",
                        len(artifact.manifest.sha256),
                    )
                except Exception as e:
                    logger.error("    ERROR: Artifact validation failed: %s", e)
                    all_valid = False
                    continue

        logger.info("")
        if all_valid:
            logger.info("✓ All checks passed")
            return True
        else:
            logger.error("✗ Some checks failed")
            return False

    except RegistryError as e:
        logger.error("Registry validation failed: %s", e)
        return False
    except FileNotFoundError as e:
        logger.error("File not found: %s", e)
        return False
    except Exception:
        logger.exception("Unexpected error during verification")
        return False


def main() -> int:
    """Main entry point.

    Returns:
        0 if all checks pass, 1 otherwise
    """
    parser = argparse.ArgumentParser(
        description="Verify ML model registry and referenced artifacts"
    )
    parser.add_argument(
        "--path",
        type=Path,
        required=True,
        help="Path to registry JSON file (e.g., ml/registry/models.json)",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory for resolving artifact paths (defaults to registry parent)",
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

    success = verify_registry(args.path, args.base_dir)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
