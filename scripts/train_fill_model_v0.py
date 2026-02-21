#!/usr/bin/env python3
"""Train a fill probability model v0 (Track C, PR-C2).

Reads a fill_outcomes_v1 dataset (data.parquet + manifest.json),
trains a calibrated bin-count model, and writes model.json + manifest.json.

Requires pyarrow (pip install grinder[dev] or grinder[ml]).

Usage:
    python3 -m scripts.train_fill_model_v0 --dataset ml/datasets/fill_outcomes/v1/fill_outcomes_v1 --out-dir ml/models/fill_model_v0
    python3 -m scripts.train_fill_model_v0 --dataset <dir> --out-dir <dir> --force

Exit codes:
    0 - Success
    1 - Error (missing deps, bad dataset, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

try:
    import pyarrow.parquet as pq
except ImportError:
    print(
        "ERROR: pyarrow required. Install with: pip install grinder[dev]",
        file=sys.stderr,
    )
    sys.exit(1)

from grinder.ml.fill_dataset import FillOutcomeRow
from grinder.ml.fill_model_v0 import FillModelV0


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_dataset(dataset_dir: Path) -> list[FillOutcomeRow]:
    """Load FillOutcomeRow objects from a fill_outcomes_v1 dataset.

    Validates manifest sha256 against data.parquet.

    Raises:
        FileNotFoundError: If manifest.json or data.parquet missing.
        ValueError: If sha256 mismatch.
    """
    manifest_path = dataset_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    data_path = dataset_dir / "data.parquet"
    expected_sha = manifest["sha256"]["data.parquet"]
    actual_sha = _sha256_file(data_path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"SHA256 mismatch for data.parquet: expected {expected_sha}, got {actual_sha}"
        )

    table = pq.read_table(data_path)
    rows: list[FillOutcomeRow] = []
    for i in range(table.num_rows):
        rows.append(
            FillOutcomeRow(
                row_id=str(table.column("row_id")[i].as_py()),
                symbol=str(table.column("symbol")[i].as_py()),
                direction=str(table.column("direction")[i].as_py()),
                entry_ts=int(table.column("entry_ts")[i].as_py()),
                entry_price=Decimal(str(table.column("entry_price")[i].as_py())),
                entry_qty=Decimal(str(table.column("entry_qty")[i].as_py())),
                entry_fee=Decimal(str(table.column("entry_fee")[i].as_py())),
                entry_fill_count=int(table.column("entry_fill_count")[i].as_py()),
                exit_ts=int(table.column("exit_ts")[i].as_py()),
                exit_price=Decimal(str(table.column("exit_price")[i].as_py())),
                exit_qty=Decimal(str(table.column("exit_qty")[i].as_py())),
                exit_fee=Decimal(str(table.column("exit_fee")[i].as_py())),
                exit_fill_count=int(table.column("exit_fill_count")[i].as_py()),
                realized_pnl=Decimal(str(table.column("realized_pnl")[i].as_py())),
                net_pnl=Decimal(str(table.column("net_pnl")[i].as_py())),
                pnl_bps=int(table.column("pnl_bps")[i].as_py()),
                holding_time_ms=int(table.column("holding_time_ms")[i].as_py()),
                notional=Decimal(str(table.column("notional")[i].as_py())),
                outcome=str(table.column("outcome")[i].as_py()),
                source=str(table.column("source")[i].as_py()),
                dataset_version=str(table.column("dataset_version")[i].as_py()),
            )
        )

    return rows


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Train fill probability model v0 (Track C, PR-C2)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to fill_outcomes_v1 dataset directory",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory for model artifact",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing model directory",
    )
    parser.add_argument(
        "--created-at-utc",
        type=str,
        default=None,
        help="Override timestamp (for deterministic builds)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed progress",
    )

    args = parser.parse_args()

    try:
        # Load dataset.
        rows = _load_dataset(args.dataset)
        if args.verbose:
            print(f"Loaded {len(rows)} roundtrips from {args.dataset}")

        # Train model.
        model = FillModelV0.train(rows)
        if args.verbose:
            print(
                f"Trained model: {len(model.bins)} bins, "
                f"global prior = {model.global_prior_bps} bps"
            )

        # Save artifact.
        model_dir = model.save(
            args.out_dir,
            force=args.force,
            created_at_utc=args.created_at_utc,
        )

        print(f"OK: Fill model v0 saved to {model_dir}")
        print(f"  Bins: {len(model.bins)}")
        print(f"  Global prior: {model.global_prior_bps} bps")
        print(f"  Train rows: {model.n_train_rows}")
        print("  Files: model.json, manifest.json")
        return 0

    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
