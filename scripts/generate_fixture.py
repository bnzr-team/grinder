#!/usr/bin/env python3
"""
Generate synthetic fixture data for replay testing.

Usage:
    python -m scripts.generate_fixture --symbols BTCUSDT --duration-s 2 --out-dir /tmp/fixture
"""

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any


def generate_events(symbols: list[str], duration_s: int, seed: int = 42) -> list[dict[str, Any]]:
    """Generate synthetic market events."""
    random.seed(seed)
    events = []
    ts = 1000  # Start timestamp

    for _ in range(duration_s * 10):  # 10 events per second
        for symbol in symbols:
            mid = 50000.0 if symbol == "BTCUSDT" else 3000.0
            spread = mid * 0.0001  # 1 bps spread

            events.append(
                {
                    "ts": ts,
                    "type": "BOOK_TICKER",
                    "symbol": symbol,
                    "bid": mid - spread / 2,
                    "ask": mid + spread / 2,
                    "bid_qty": round(random.uniform(0.1, 10.0), 4),
                    "ask_qty": round(random.uniform(0.1, 10.0), 4),
                }
            )

            if random.random() < 0.3:  # 30% chance of trade
                events.append(
                    {
                        "ts": ts + random.randint(0, 50),
                        "type": "TRADE",
                        "symbol": symbol,
                        "price": mid + random.uniform(-spread, spread),
                        "qty": round(random.uniform(0.01, 1.0), 4),
                        "side": random.choice(["BUY", "SELL"]),
                    }
                )

        ts += 100  # 100ms increments

    return sorted(events, key=lambda e: e["ts"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic fixture")
    parser.add_argument(
        "--symbols",
        type=str,
        default="BTCUSDT",
        help="Comma-separated symbols",
    )
    parser.add_argument(
        "--duration-s",
        type=int,
        default=2,
        help="Duration in seconds",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    out_dir = args.out_dir

    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating fixture for {symbols} ({args.duration_s}s)")

    events = generate_events(symbols, args.duration_s, args.seed)

    # Write events.jsonl
    events_path = out_dir / "events.jsonl"
    with events_path.open("w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")

    print(f"Generated {len(events)} events -> {events_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
