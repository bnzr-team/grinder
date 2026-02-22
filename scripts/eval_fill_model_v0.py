#!/usr/bin/env python3
"""Offline evaluation & threshold calibration for FillModelV0 (Track C, PR-C7).

Reads a fill_outcomes_v1 dataset + trained FillModelV0 model, runs a
101-point threshold sweep (0..10000 bps, step 100), computes calibration
diagnostics, and writes a deterministic JSON report artifact.

Requires pyarrow (pip install grinder[dev] or grinder[ml]).

Usage:
    python3 -m scripts.eval_fill_model_v0 \\
        --dataset ml/datasets/fill_outcomes/v1/fill_outcomes_v1 \\
        --model ml/models/fill_model_v0 \\
        --out-dir ml/eval/fill_model_v0

    python3 -m scripts.eval_fill_model_v0 \\
        --dataset <dir> --model <dir> --out-dir <dir> \\
        --cost-ratio 3.0 --force

Exit codes:
    0 - Success
    1 - Error (missing deps, bad dataset/model, etc.)
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
from grinder.ml.fill_model_eval import EvalReport, evaluate_fill_model, write_eval_report
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


def _print_summary(report: EvalReport) -> None:
    """Print human-readable evaluation summary to stdout."""
    print(f"Dataset: {report.dataset_path} ({report.n_rows} rows)")
    print(f"  Wins: {report.n_wins}, Losses: {report.n_losses}, Breakeven: {report.n_breakeven}")
    print(f"Model: {report.model_path} (global prior: {report.global_prior_bps} bps)")
    print(f"Cost ratio: {report.cost_ratio}")
    print()

    # Find recommended threshold entry
    rec = None
    for entry in report.threshold_sweep:
        if entry.threshold_bps == report.recommended_threshold_bps:
            rec = entry
            break

    print(f"Recommended threshold: {report.recommended_threshold_bps} bps")
    if rec:
        print(f"  Block rate: {rec.block_rate_pct:.1f}%")
        print(f"  Precision: {rec.precision_pct:.1f}%")
        print(f"  Recall: {rec.recall_pct:.1f}%")
        print(f"  F1: {rec.f1_pct:.1f}%")
        print(f"  Cost score: {rec.cost_score:.2f}")
    print()

    if report.calibration_well_calibrated:
        print(
            f"Calibration: well-calibrated (max error: {report.calibration_max_error_bps} bps < 500 bps)"
        )
    else:
        print(
            f"Calibration: NOT well-calibrated (max error: {report.calibration_max_error_bps} bps >= 500 bps)"
        )


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Offline evaluation & threshold calibration for FillModelV0 (Track C, PR-C7)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to fill_outcomes_v1 dataset directory",
    )
    parser.add_argument(
        "--model",
        type=Path,
        required=True,
        help="Path to FillModelV0 model directory",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory for evaluation report artifact",
    )
    parser.add_argument(
        "--cost-ratio",
        type=float,
        default=2.0,
        help="Cost ratio: value of blocking a loss relative to allowing a win (default: 2.0)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output directory",
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

        # Load model.
        model = FillModelV0.load(args.model)
        if args.verbose:
            print(
                f"Loaded model: {len(model.bins)} bins, global prior = {model.global_prior_bps} bps"
            )

        # Read manifest sha256s for provenance.
        dataset_manifest_path = args.dataset / "manifest.json"
        dataset_manifest_sha256 = _sha256_file(dataset_manifest_path)

        model_manifest_path = args.model / "manifest.json"
        model_manifest_sha256 = _sha256_file(model_manifest_path)

        # Run evaluation.
        report = evaluate_fill_model(
            rows=rows,
            model=model,
            cost_ratio=args.cost_ratio,
            dataset_path=str(args.dataset),
            model_path=str(args.model),
            dataset_manifest_sha256=dataset_manifest_sha256,
            model_manifest_sha256=model_manifest_sha256,
        )

        if args.verbose:
            print(f"Threshold sweep: {len(report.threshold_sweep)} entries")
            print(f"Calibration bins: {len(report.calibration)} bins")

        # Write artifact.
        report_path, manifest_path = write_eval_report(
            report,
            args.out_dir,
            force=args.force,
        )

        # Print summary.
        _print_summary(report)
        print()
        print(f"OK: Evaluation report saved to {args.out_dir}")
        print(f"  Files: {report_path.name}, {manifest_path.name}")
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
