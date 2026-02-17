#!/usr/bin/env python3
"""Build a dataset artifact in Feature Store spec v1 format.

M8-04b: Generates synthetic datasets with deterministic data,
writes data.parquet + manifest.json, and self-validates via verify_dataset.

Usage:
    python -m scripts.build_dataset --out-dir ml/datasets --dataset-id synthetic_v1 --source synthetic --rows 200 --seed 42
    python -m scripts.build_dataset --out-dir ml/datasets --dataset-id synthetic_v1 --source synthetic --rows 200 --seed 42 --force

Exit codes:
    0 - Success (dataset built and verified)
    1 - Build or validation error
    2 - Usage error
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from grinder.ml.onnx.features import FEATURE_ORDER
from scripts.verify_dataset import verify_dataset

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SOURCES = frozenset({"synthetic", "backtest", "export", "manual"})

LABEL_COLUMNS = ("regime", "spacing_multiplier")

# Regime labels: 0=LOW, 1=MID, 2=HIGH
_N_REGIMES = 3


# ---------------------------------------------------------------------------
# Data generation
# ---------------------------------------------------------------------------


def _generate_synthetic_data(
    n_rows: int,
    seed: int,
) -> pa.Table:
    """Generate deterministic synthetic feature + label data.

    Args:
        n_rows: Number of rows.
        seed: Random seed for reproducibility.

    Returns:
        pyarrow Table with FEATURE_ORDER columns + LABEL_COLUMNS.
    """
    rng = np.random.default_rng(seed)

    arrays: dict[str, pa.Array] = {}

    for feat in FEATURE_ORDER:
        values = rng.standard_normal(n_rows).astype(np.float32)
        arrays[feat] = pa.array(values, type=pa.float32())

    # Labels
    arrays["regime"] = pa.array(
        rng.integers(0, _N_REGIMES, size=n_rows).astype(np.int32),
        type=pa.int32(),
    )
    arrays["spacing_multiplier"] = pa.array(
        (1.0 + rng.random(n_rows) * 2.0).astype(np.float32),
        type=pa.float32(),
    )

    return pa.table(arrays)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def _compute_feature_order_hash() -> str:
    """Compute feature_order_hash from SSOT FEATURE_ORDER."""
    return hashlib.sha256(json.dumps(list(FEATURE_ORDER)).encode()).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_manifest(
    dataset_id: str,
    source: str,
    row_count: int,
    seed: int | None,
    dataset_dir: Path,
    created_at_utc: str | None = None,
) -> dict[str, object]:
    """Build manifest dict conforming to Feature Store spec v1.

    Args:
        dataset_id: Dataset identifier.
        source: Source type (synthetic/backtest/export/manual).
        row_count: Number of rows in data.parquet.
        seed: Random seed (required for synthetic).
        dataset_dir: Path to dataset directory (for SHA256 computation).
        created_at_utc: Override timestamp (for deterministic tests).

    Returns:
        Manifest dict ready for JSON serialization.
    """
    if created_at_utc is None:
        created_at_utc = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    sha256_map: dict[str, str] = {}
    data_path = dataset_dir / "data.parquet"
    if data_path.exists():
        sha256_map["data.parquet"] = _sha256_file(data_path)

    manifest: dict[str, object] = {
        "schema_version": "v1",
        "dataset_id": dataset_id,
        "created_at_utc": created_at_utc,
        "source": source,
        "feature_order": list(FEATURE_ORDER),
        "feature_order_hash": _compute_feature_order_hash(),
        "label_columns": list(LABEL_COLUMNS),
        "row_count": row_count,
        "sha256": sha256_map,
    }

    if source == "synthetic" and seed is not None:
        manifest["determinism"] = {
            "seed": seed,
            "build_command": (
                f"python -m scripts.build_dataset"
                f" --out-dir {dataset_dir.parent}"
                f" --dataset-id {dataset_id}"
                f" --source {source}"
                f" --rows {row_count}"
                f" --seed {seed}"
            ),
        }

    return manifest


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_dataset(
    out_dir: Path,
    dataset_id: str,
    source: str,
    rows: int,
    seed: int,
    *,
    force: bool = False,
    created_at_utc: str | None = None,
    verbose: bool = False,
) -> Path:
    """Build a dataset artifact (data.parquet + manifest.json).

    Args:
        out_dir: Parent directory for datasets.
        dataset_id: Dataset identifier.
        source: Source type.
        rows: Number of rows.
        seed: Random seed.
        force: Overwrite existing dataset directory.
        created_at_utc: Override timestamp (for deterministic tests).
        verbose: Print progress.

    Returns:
        Path to the created dataset directory.

    Raises:
        FileExistsError: If dataset directory exists and force=False.
        RuntimeError: If self-verification fails after build.
    """
    dataset_dir = out_dir / dataset_id

    # Fail-closed: refuse to overwrite without --force
    if dataset_dir.exists():
        if not force:
            raise FileExistsError(
                f"Dataset directory already exists: {dataset_dir} (use --force to overwrite)"
            )
        if verbose:
            print(f"Removing existing: {dataset_dir}")
        shutil.rmtree(dataset_dir)

    dataset_dir.mkdir(parents=True)

    if verbose:
        print(f"Building dataset: {dataset_id}")
        print(f"  Source: {source} (rows={rows}, seed={seed})")

    # 1. Generate data
    table = _generate_synthetic_data(rows, seed)

    # 2. Write parquet (deterministic: sorted columns, snappy compression)
    data_path = dataset_dir / "data.parquet"
    pq.write_table(
        table,
        data_path,
        compression="snappy",
        write_statistics=False,
    )

    if verbose:
        print(f"  Wrote: {data_path} ({data_path.stat().st_size} bytes)")

    # 3. Build and write manifest
    manifest = _build_manifest(
        dataset_id=dataset_id,
        source=source,
        row_count=rows,
        seed=seed,
        dataset_dir=dataset_dir,
        created_at_utc=created_at_utc,
    )

    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    if verbose:
        foh = manifest.get("feature_order_hash", "?")
        print(f"  Wrote: {manifest_path}")
        print(f"  Feature order hash: {foh}")
        sha_map = manifest.get("sha256", {})
        if isinstance(sha_map, dict):
            for fname, sha in sha_map.items():
                print(f"  SHA256: {fname} = {sha[:16]}...")

    # 4. Self-verify (fail-closed)
    if verbose:
        print("  Running self-verification...")

    errors = verify_dataset(manifest_path, base_dir=out_dir, verbose=False)
    if errors:
        # Clean up on failure (best effort)
        shutil.rmtree(dataset_dir, ignore_errors=True)
        msg = f"Self-verification failed ({len(errors)} errors):\n"
        for err in errors:
            msg += f"  - {err}\n"
        raise RuntimeError(msg)

    if verbose:
        print("  Self-verification: PASS")
        print(f"Dataset built: {dataset_dir}/")

    return dataset_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Main entry point.

    Returns:
        0 if success, 1 if build/validation error, 2 if usage error.
    """
    parser = argparse.ArgumentParser(
        description="Build a dataset artifact (M8-04b)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m scripts.build_dataset --out-dir ml/datasets --dataset-id synthetic_v1 --source synthetic --rows 200 --seed 42
    python -m scripts.build_dataset --out-dir ml/datasets --dataset-id my_data --source synthetic --rows 1000 --seed 123 --force -v
""",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Parent directory for datasets (e.g., ml/datasets)",
    )
    parser.add_argument(
        "--dataset-id",
        type=str,
        required=True,
        help="Dataset identifier (e.g., synthetic_v1)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="synthetic",
        choices=sorted(VALID_SOURCES),
        help="Data source type (default: synthetic)",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=200,
        help="Number of rows to generate (default: 200)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing dataset directory",
    )
    parser.add_argument(
        "--created-at-utc",
        type=str,
        default=None,
        help="Override created_at_utc timestamp (for deterministic tests)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed progress",
    )

    args = parser.parse_args()

    try:
        dataset_dir = build_dataset(
            out_dir=args.out_dir,
            dataset_id=args.dataset_id,
            source=args.source,
            rows=args.rows,
            seed=args.seed,
            force=args.force,
            created_at_utc=args.created_at_utc,
            verbose=args.verbose,
        )
        print(f"OK: Dataset built at {dataset_dir}")
        return 0

    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
