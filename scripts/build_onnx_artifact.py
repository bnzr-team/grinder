#!/usr/bin/env python3
"""Build ONNX artifact with v1.1 manifest.

M8-03a: Create artifact directory with model, manifest.json, and checksums.

Usage:
    python -m scripts.build_onnx_artifact --model-path model.onnx --output-dir artifacts/v1 --dataset-id train_v1
    python -m scripts.build_onnx_artifact --help

Exit codes:
    0 - Artifact built successfully
    1 - Build failed
    2 - Usage error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from grinder.ml.onnx.features import FEATURE_ORDER
from grinder.ml.onnx.types import ARTIFACT_SCHEMA_VERSION

# Chunk size for SHA256 calculation (4MB)
SHA256_CHUNK_SIZE = 4 * 1024 * 1024


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of a file in chunks.

    Args:
        file_path: Path to the file.

    Returns:
        Lowercase hex digest (64 characters).
    """
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(SHA256_CHUNK_SIZE):
            sha256.update(chunk)
    return sha256.hexdigest().lower()


def get_git_sha() -> str | None:
    """Get current git commit SHA.

    Returns:
        40-character git SHA or None if unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            # Validate format
            if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower()):
                return sha.lower()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def build_artifact(
    model_path: Path,
    output_dir: Path,
    dataset_id: str,
    notes: str | None = None,
    verbose: bool = False,
) -> Path:
    """Build ONNX artifact with v1.1 manifest.

    Args:
        model_path: Path to source ONNX model file.
        output_dir: Directory to create artifact in.
        dataset_id: Training dataset identifier.
        notes: Optional human-readable notes.
        verbose: Print detailed progress.

    Returns:
        Path to created artifact directory.

    Raises:
        ValueError: If model file doesn't exist or output dir exists.
        OSError: If file operations fail.
    """
    # Validate inputs
    if not model_path.exists():
        raise ValueError(f"Model file not found: {model_path}")
    if not model_path.is_file():
        raise ValueError(f"Model path is not a file: {model_path}")
    if output_dir.exists():
        raise ValueError(f"Output directory already exists: {output_dir}")

    # Create output directory
    output_dir.mkdir(parents=True)
    if verbose:
        print(f"Created output directory: {output_dir}")

    # Copy model file
    model_name = "model.onnx"
    dest_model = output_dir / model_name
    shutil.copy2(model_path, dest_model)
    if verbose:
        print(f"Copied model: {model_path} -> {dest_model}")

    # Compute SHA256
    model_sha = compute_sha256(dest_model)
    if verbose:
        print(f"SHA256: {model_sha}")

    # Get git SHA (graceful fallback)
    git_sha = get_git_sha()
    if git_sha is None:
        print("WARNING: Could not determine git SHA (not in git repo or git unavailable)")
    elif verbose:
        print(f"Git SHA: {git_sha}")

    # Generate timestamps
    now_utc = datetime.now(UTC)
    created_at_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    created_at = created_at_utc  # Use same format for both

    # Build manifest
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model_file": model_name,
        "sha256": {
            model_name: model_sha,
        },
        "created_at": created_at,
        "created_at_utc": created_at_utc,
        "dataset_id": dataset_id,
        "feature_order": list(FEATURE_ORDER),
    }

    if git_sha is not None:
        manifest["git_sha"] = git_sha

    if notes is not None:
        manifest["notes"] = notes

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    if verbose:
        print(f"Wrote manifest: {manifest_path}")

    return output_dir


def main() -> int:
    """Build ONNX artifact and return exit code."""
    parser = argparse.ArgumentParser(
        description="Build ONNX artifact with v1.1 manifest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m scripts.build_onnx_artifact \\
        --model-path trained_model.onnx \\
        --output-dir artifacts/regime_v1 \\
        --dataset-id train_2026Q1

    python -m scripts.build_onnx_artifact \\
        --model-path model.onnx \\
        --output-dir artifacts/test \\
        --dataset-id ci_fixture \\
        --notes "Test artifact for CI"
""",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        required=True,
        help="Path to source ONNX model file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to create artifact in (must not exist)",
    )
    parser.add_argument(
        "--dataset-id",
        type=str,
        required=True,
        help="Training dataset identifier (e.g., 'train_2026Q1')",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default=None,
        help="Optional human-readable notes",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed build info",
    )

    args = parser.parse_args()

    model_path: Path = args.model_path
    output_dir: Path = args.output_dir
    dataset_id: str = args.dataset_id
    notes: str | None = args.notes
    verbose: bool = args.verbose

    try:
        artifact_dir = build_artifact(
            model_path=model_path,
            output_dir=output_dir,
            dataset_id=dataset_id,
            notes=notes,
            verbose=verbose,
        )

        print(f"OK: Artifact built at {artifact_dir}")
        return 0

    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    except OSError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
