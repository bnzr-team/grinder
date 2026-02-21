#!/usr/bin/env python3
"""Build a fill outcome dataset artifact (Track C, PR-C1).

Reads a JSON fixture of Fill objects, runs them through the
RoundtripTracker, and writes data.parquet + manifest.json.

Usage:
    python3 -m scripts.build_fill_dataset_v1 --fixture tests/fixtures/fills.json --out-dir ml/datasets/fill_outcomes/v1
    python3 -m scripts.build_fill_dataset_v1 --fixture tests/fixtures/fills.json --out-dir ml/datasets/fill_outcomes/v1 --force

Exit codes:
    0 - Success
    1 - Build error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from grinder.ml.fill_dataset import RoundtripTracker, build_fill_dataset_v1
from grinder.paper.fills import Fill


def _load_fills(fixture_path: Path) -> list[Fill]:
    """Load Fill objects from a JSON fixture file.

    Expected format: list of dicts with keys:
    ts, symbol, side, price, quantity, order_id
    """
    raw = json.loads(fixture_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"Expected list of fills, got {type(raw).__name__}")
    return [Fill.from_dict(d) for d in raw]


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Build fill outcome dataset v1 (Track C, PR-C1)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="Path to JSON fixture of Fill objects",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Parent directory for dataset output",
    )
    parser.add_argument(
        "--dataset-id",
        type=str,
        default="fill_outcomes_v1",
        help="Dataset identifier (default: fill_outcomes_v1)",
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
        # Load fills
        fills = _load_fills(args.fixture)
        if args.verbose:
            print(f"Loaded {len(fills)} fills from {args.fixture}")

        # Run through roundtrip tracker
        tracker = RoundtripTracker(source="paper")
        rows = []
        for fill in fills:
            row = tracker.record(fill)
            if row is not None:
                rows.append(row)

        if args.verbose:
            print(f"Detected {len(rows)} completed roundtrips")
            open_pos = tracker.open_positions
            if open_pos:
                print(f"  ({len(open_pos)} positions still open, not emitted)")

        # Build dataset
        dataset_dir = build_fill_dataset_v1(
            rows=rows,
            out_dir=args.out_dir,
            dataset_id=args.dataset_id,
            force=args.force,
            created_at_utc=args.created_at_utc,
        )

        print(f"OK: Fill dataset built at {dataset_dir}")
        print(f"  Rows: {len(rows)}")
        print("  Files: data.parquet, manifest.json")
        return 0

    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
