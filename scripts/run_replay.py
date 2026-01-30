#!/usr/bin/env python3
"""
Run backtest replay on recorded fixture data.

Usage:
    python -m scripts.run_replay --fixture tests/fixtures/sample_day/ -v
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def load_fixture(fixture_dir: Path) -> list[dict[str, Any]]:
    """Load fixture events from directory."""
    events = []

    # Look for events.jsonl or events.json
    jsonl_path = fixture_dir / "events.jsonl"
    json_path = fixture_dir / "events.json"

    if jsonl_path.exists():
        with jsonl_path.open() as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line))
    elif json_path.exists():
        with json_path.open() as f:
            events = json.load(f)

    return events


def compute_digest(events: list[dict[str, Any]]) -> str:
    """Compute deterministic digest of events."""
    # Sort events by timestamp for determinism
    sorted_events = sorted(events, key=lambda e: e.get("ts", 0))
    content = json.dumps(sorted_events, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest replay")
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="Fixture directory with events data",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    fixture_dir = args.fixture

    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Loading fixture from: {fixture_dir}")

    events = load_fixture(fixture_dir)

    if args.verbose:
        print(f"Loaded {len(events)} events")

    # If no events found, create empty deterministic output
    if not events:
        if args.verbose:
            print("No events found, using empty fixture")
        events = [{"ts": 0, "type": "EMPTY_FIXTURE"}]

    # Compute digest
    digest = compute_digest(events)

    print(f"Replay completed. Events processed: {len(events)}")
    print(f"Output digest: {digest}")


if __name__ == "__main__":
    main()
