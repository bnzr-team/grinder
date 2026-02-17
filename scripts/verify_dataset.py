#!/usr/bin/env python3
"""Verify dataset manifest integrity.

M8-04a: Validates dataset manifest schema, path safety, feature_order_hash,
and SHA256 checksums. Fail-closed: any validation error -> exit 1.

Usage:
    python -m scripts.verify_dataset --path ml/datasets/<id>/manifest.json
    python -m scripts.verify_dataset --path ml/datasets/<id>/manifest.json --base-dir .
    python -m scripts.verify_dataset --path ml/datasets/<id>/manifest.json -v

Exit codes:
    0 - All checks passed
    1 - Validation failed
    2 - Usage error (file not found, invalid args)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "v1"

# Pattern: starts with [a-z0-9], then [a-z0-9._-]{2,64}  -> total 3..65 chars
DATASET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,64}$")

VALID_SOURCES = frozenset({"synthetic", "backtest", "export", "manual"})

MAX_ROW_COUNT = 10_000_000
MIN_ROW_COUNT = 10
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_DIR_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB total

# Files that MUST exist in every dataset directory (per spec S3)
REQUIRED_FILES = frozenset({"data.parquet"})

# ISO 8601 UTC timestamp pattern: YYYY-MM-DDTHH:MM:SSZ (with optional fractional seconds)
_ISO8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

# Required top-level keys in manifest
REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "created_at_utc",
        "source",
        "feature_order",
        "feature_order_hash",
        "label_columns",
        "row_count",
        "sha256",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DatasetValidationError(Exception):
    """Raised when dataset manifest validation fails."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _get_ssot_feature_order() -> list[str]:
    """Return SSOT FEATURE_ORDER as a list.

    Returns:
        List of feature names from grinder.ml.onnx.features.FEATURE_ORDER.
    """
    from grinder.ml.onnx.features import FEATURE_ORDER  # noqa: PLC0415

    return list(FEATURE_ORDER)


def _compute_feature_order_hash() -> str:
    """Compute feature_order_hash from SSOT FEATURE_ORDER.

    Returns:
        16-char hex string: SHA256(json.dumps(list(FEATURE_ORDER)))[:16]
    """
    return hashlib.sha256(json.dumps(_get_ssot_feature_order()).encode()).hexdigest()[:16]


def _validate_path_safety(path_str: str) -> None:
    """Validate that a path is safe (relative, no traversal, no absolute).

    Args:
        path_str: Path string to validate.

    Raises:
        DatasetValidationError: If path is unsafe.
    """
    p = Path(path_str)

    if p.is_absolute():
        raise DatasetValidationError(f"Absolute path not allowed: {path_str!r}")

    if ".." in p.parts:
        raise DatasetValidationError(f"Path traversal (..) not allowed: {path_str!r}")


def _check_no_symlink(path: Path) -> None:
    """Reject symlinks (resolved path must equal unresolved path).

    Args:
        path: File path to check.

    Raises:
        DatasetValidationError: If path is or contains a symlink.
    """
    if path.is_symlink():
        raise DatasetValidationError(f"Symlink not allowed: {path}")
    # Also check that resolve() doesn't change the path (catches symlinks in parents)
    try:
        if path.exists() and path.resolve() != path.absolute():
            raise DatasetValidationError(
                f"Path resolves differently (possible symlink in chain): {path} -> {path.resolve()}"
            )
    except OSError:
        pass  # Path doesn't exist yet -- caller handles existence check


def _check_containment(child: Path, parent: Path) -> None:
    """Verify child path is contained within parent.

    Args:
        child: Resolved child path.
        parent: Resolved parent path.

    Raises:
        DatasetValidationError: If child escapes parent.
    """
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        raise DatasetValidationError(
            f"Path escapes base directory: {child} is not under {parent}"
        ) from None


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file.

    Args:
        path: File path.

    Returns:
        64-char hex string.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------


def verify_dataset(  # noqa: PLR0912, PLR0915
    manifest_path: Path,
    base_dir: Path | None = None,
    *,
    verbose: bool = False,
) -> list[str]:
    """Verify dataset manifest and referenced files.

    Args:
        manifest_path: Path to manifest.json file.
        base_dir: Base directory for containment check (defaults to manifest parent).
        verbose: If True, print detailed progress.

    Returns:
        List of error messages. Empty list = all checks passed.
    """
    errors: list[str] = []
    info: list[str] = []

    def _info(msg: str) -> None:
        info.append(msg)
        if verbose:
            print(msg)

    def _fail(msg: str) -> None:
        errors.append(msg)
        if verbose:
            print(f"  FAIL: {msg}")

    # --- 1. Load manifest ---
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return [f"Manifest not found: {manifest_path}"]

    if not manifest_path.is_file():
        return [f"Manifest is not a file: {manifest_path}"]

    try:
        with manifest_path.open() as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        return [f"Invalid JSON in manifest: {e}"]

    if not isinstance(manifest, dict):
        return [f"Manifest root must be object, got {type(manifest).__name__}"]

    dataset_dir = manifest_path.parent
    if base_dir is None:
        base_dir = dataset_dir

    _info(f"Loading dataset: {manifest_path}")

    # --- 2. Required keys ---
    missing = REQUIRED_KEYS - set(manifest.keys())
    if missing:
        _fail(f"Missing required keys: {sorted(missing)}")
        return errors  # Can't continue without required keys

    # --- 3. schema_version ---
    sv = manifest["schema_version"]
    if sv != SCHEMA_VERSION:
        _fail(f"schema_version must be {SCHEMA_VERSION!r}, got {sv!r}")
    else:
        _info(f"  Schema version: {sv}")

    # --- 4. dataset_id ---
    did = manifest["dataset_id"]
    if not isinstance(did, str) or not DATASET_ID_PATTERN.match(did):
        _fail(f"Invalid dataset_id: {did!r} (must match {DATASET_ID_PATTERN.pattern})")
    else:
        _info(f"  Dataset ID: {did}")
        # Verify directory name matches dataset_id
        if dataset_dir.name != did:
            _fail(f"Directory name {dataset_dir.name!r} does not match dataset_id {did!r}")

    # --- 5. source ---
    source = manifest["source"]
    if source not in VALID_SOURCES:
        _fail(f"Invalid source: {source!r} (must be one of {sorted(VALID_SOURCES)})")
    else:
        _info(f"  Source: {source}")

    # --- 6. row_count ---
    row_count = manifest["row_count"]
    if not isinstance(row_count, int):
        _fail(f"row_count must be int, got {type(row_count).__name__}")
    elif row_count < MIN_ROW_COUNT:
        _fail(f"row_count {row_count} below minimum {MIN_ROW_COUNT}")
    elif row_count > MAX_ROW_COUNT:
        _fail(f"row_count {row_count} exceeds maximum {MAX_ROW_COUNT}")
    else:
        _info(f"  Row count: {row_count}")

    # --- 7. feature_order (list equality vs SSOT) ---
    fo = manifest["feature_order"]
    if not isinstance(fo, list) or not all(isinstance(f, str) for f in fo):
        _fail("feature_order must be a list of strings")
    else:
        ssot_fo = _get_ssot_feature_order()
        if fo != ssot_fo:
            _fail(
                f"feature_order mismatch vs SSOT FEATURE_ORDER: "
                f"manifest has {len(fo)} features {fo}, "
                f"SSOT has {len(ssot_fo)} features {ssot_fo}"
            )
        else:
            _info(f"  Feature order: {len(fo)} features (matches FEATURE_ORDER)")

    # --- 8. feature_order_hash ---
    foh = manifest["feature_order_hash"]
    if not isinstance(foh, str):
        _fail(f"feature_order_hash must be string, got {type(foh).__name__}")
    else:
        expected_hash = _compute_feature_order_hash()
        if foh != expected_hash:
            _fail(
                f"feature_order_hash mismatch: manifest={foh!r}, "
                f"expected={expected_hash!r} (from FEATURE_ORDER SSOT)"
            )
        else:
            _info(f"  Feature order hash: {foh} (matches FEATURE_ORDER)")

    # --- 9. label_columns ---
    lc = manifest["label_columns"]
    if not isinstance(lc, list) or not all(isinstance(c, str) for c in lc):
        _fail("label_columns must be a list of strings")

    # --- 10. Required files (spec S3: data.parquet MUST exist) ---
    for req_file in sorted(REQUIRED_FILES):
        req_path = dataset_dir / req_file
        if not req_path.exists():
            _fail(f"Required file missing: {req_file}")
        elif req_path.is_symlink():
            _fail(f"Required file is a symlink (not allowed): {req_file}")

    # --- 11. Path safety + SHA256 integrity for sha256 entries ---
    sha_map = manifest["sha256"]
    if not isinstance(sha_map, dict):
        _fail(f"sha256 must be object, got {type(sha_map).__name__}")
    else:
        _info("  SHA256 check:")
        for filename, expected_sha in sha_map.items():
            # Path safety
            try:
                _validate_path_safety(filename)
            except DatasetValidationError as e:
                _fail(f"sha256 key path unsafe: {e}")
                continue

            # Validate SHA format (64-char hex)
            if not isinstance(expected_sha, str) or not re.match(r"^[0-9a-f]{64}$", expected_sha):
                _fail(f"Invalid SHA256 value for {filename!r}: must be 64-char hex")
                continue

            # Resolve and check containment
            file_path = (dataset_dir / filename).resolve()
            try:
                _check_containment(file_path, base_dir.resolve())
            except DatasetValidationError as e:
                _fail(str(e))
                continue

            # File exists?
            if not file_path.exists():
                _fail(f"File not found: {filename}")
                continue

            if not file_path.is_file():
                _fail(f"Not a file: {filename}")
                continue

            # Symlink check
            try:
                _check_no_symlink(dataset_dir / filename)
            except DatasetValidationError as e:
                _fail(str(e))
                continue

            # Size check
            file_size = file_path.stat().st_size
            if file_size > MAX_FILE_SIZE_BYTES:
                _fail(f"File {filename} size {file_size} bytes exceeds limit {MAX_FILE_SIZE_BYTES}")
                continue

            # SHA256 integrity
            actual_sha = _sha256_file(file_path)
            if actual_sha != expected_sha:
                _fail(
                    f"SHA256 mismatch for {filename}: "
                    f"manifest={expected_sha[:16]}..., "
                    f"actual={actual_sha[:16]}..."
                )
            else:
                _info(f"    {filename}: {actual_sha[:16]}... OK")

    # --- 12. created_at_utc format (ISO 8601 UTC: YYYY-MM-DDTHH:MM:SS[.f]Z) ---
    cat = manifest["created_at_utc"]
    if not isinstance(cat, str) or not _ISO8601_UTC_RE.match(cat):
        _fail(f"created_at_utc must be ISO 8601 UTC (YYYY-MM-DDTHH:MM:SS[.f]Z), got {cat!r}")

    # --- 13. Total directory size limit ---
    total_size = 0
    for item in dataset_dir.rglob("*"):
        if item.is_file():
            total_size += item.stat().st_size
    if total_size > MAX_DIR_SIZE_BYTES:
        _fail(
            f"Dataset directory size {total_size} bytes "
            f"exceeds limit {MAX_DIR_SIZE_BYTES} ({MAX_DIR_SIZE_BYTES // (1024 * 1024)} MB)"
        )
    else:
        _info(f"  Directory size: {total_size} bytes OK")

    # --- Summary ---
    if not errors:
        _info("All checks passed")
    elif verbose:
        print(f"\n{len(errors)} error(s) found")

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Main entry point.

    Returns:
        0 if all checks pass, 1 if validation fails, 2 if usage error.
    """
    parser = argparse.ArgumentParser(
        description="Verify dataset manifest integrity (M8-04a)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m scripts.verify_dataset --path ml/datasets/synthetic_v1/manifest.json
    python -m scripts.verify_dataset --path ml/datasets/synthetic_v1/manifest.json --base-dir . -v
""",
    )
    parser.add_argument(
        "--path",
        type=Path,
        required=True,
        help="Path to dataset manifest.json",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory for path containment (defaults to manifest parent)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed progress",
    )

    args = parser.parse_args()

    if not args.path.exists():
        print(f"ERROR: File not found: {args.path}", file=sys.stderr)
        return 2

    errors = verify_dataset(args.path, args.base_dir, verbose=args.verbose)

    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("PASS: All dataset checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
