#!/usr/bin/env python3
"""Verify ONNX artifact integrity.

M8-02a: Validate manifest and SHA256 checksums for ONNX artifacts.

Usage:
    python -m scripts.verify_onnx_artifact <artifact_dir>
    python -m scripts.verify_onnx_artifact --help

Exit codes:
    0 - Artifact valid
    1 - Validation failed
    2 - Usage error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grinder.ml.onnx import (
    OnnxArtifactError,
    load_artifact,
)


def main() -> int:
    """Verify ONNX artifact and return exit code."""
    parser = argparse.ArgumentParser(
        description="Verify ONNX artifact integrity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m scripts.verify_onnx_artifact artifacts/model_v1
    python -m scripts.verify_onnx_artifact /path/to/artifact --verbose
""",
    )
    parser.add_argument(
        "artifact_dir",
        type=Path,
        help="Path to artifact directory containing manifest.json",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed validation info",
    )

    args = parser.parse_args()

    artifact_dir: Path = args.artifact_dir
    verbose: bool = args.verbose

    if not artifact_dir.exists():
        print(f"ERROR: Directory not found: {artifact_dir}", file=sys.stderr)
        return 2

    if not artifact_dir.is_dir():
        print(f"ERROR: Not a directory: {artifact_dir}", file=sys.stderr)
        return 2

    try:
        if verbose:
            print(f"Verifying artifact: {artifact_dir}")

        artifact = load_artifact(artifact_dir)

        if verbose:
            print(f"  Schema version: {artifact.manifest.schema_version}")
            print(f"  Model file: {artifact.manifest.model_file}")
            print(f"  Files validated: {len(artifact.manifest.sha256)}")
            for path, sha in artifact.manifest.sha256.items():
                print(f"    {path}: {sha[:16]}...")
            print(f"  Created at: {artifact.manifest.created_at}")
            if artifact.manifest.notes:
                print(f"  Notes: {artifact.manifest.notes}")

        print(f"OK: Artifact valid ({len(artifact.manifest.sha256)} files verified)")
        return 0

    except FileNotFoundError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1

    except OnnxArtifactError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
