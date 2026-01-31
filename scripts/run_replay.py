#!/usr/bin/env python3
"""
Run end-to-end backtest replay on recorded fixture data.

Usage:
    python -m scripts.run_replay --fixture tests/fixtures/sample_day/ -v

This script runs the full pipeline: prefilter -> policy -> execution
and produces a deterministic output digest for replay verification.
"""

import argparse
import json
import sys
from pathlib import Path

from grinder.replay import ReplayEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="Run end-to-end backtest replay")
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="Fixture directory with events data",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "--out",
        type=Path,
        help="Output path for replay JSON (optional)",
    )
    args = parser.parse_args()

    fixture_dir = args.fixture

    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Loading fixture from: {fixture_dir}")

    # Run end-to-end replay
    engine = ReplayEngine()
    result = engine.run(fixture_dir)

    if args.verbose:
        print(f"Events processed: {result.events_processed}")
        print(f"Outputs generated: {len(result.outputs)}")
        if result.errors:
            print(f"Errors: {len(result.errors)}")
            for err in result.errors:
                print(f"  - {err}")

    # Write output if requested
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as f:
            json.dump(result.to_dict(), f, indent=2)
        if args.verbose:
            print(f"Output written to: {args.out}")

    print(f"Replay completed. Events processed: {result.events_processed}")
    print(f"Output digest: {result.digest}")


if __name__ == "__main__":
    main()
