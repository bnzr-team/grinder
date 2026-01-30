#!/usr/bin/env python3
"""Synthetic soak runner for CI.

This repository is currently at the "skeleton" stage. The nightly soak workflow
expects JSON summaries for baseline/overload modes that can be validated by
`Scripts/check_soak_thresholds.py`.

For now we generate deterministic synthetic metrics derived from the test
parameters (number of symbols, cadence, etc.).

Usage:
  python -m scripts.run_soak --symbols BTCUSDT,ETHUSDT --duration-s 300 --cadence-ms 1000 --mode baseline --output artifacts/soak_baseline.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _compute_metrics(symbols_count: int, cadence_ms: int, mode: str) -> dict[str, float | int]:
    # load is proportional to messages per second across symbols
    msgs_per_sec = (1000.0 / max(1, cadence_ms)) * max(1, symbols_count)
    load = msgs_per_sec

    # Deterministic "p99" proxies (ms)
    decision_latency_p99_ms = round(10.0 + 1.5 * load, 2)
    order_latency_p99_ms = round(50.0 + 5.0 * load, 2)

    # Deterministic queue depth proxies
    event_queue_depth_max = int(round(load * 5))
    snapshot_queue_depth_max = int(round(load * 2))

    # Drops/errors under high load (overload allows some)
    events_dropped = max(0, int(round((load - 30) * 2)))
    errors_total = 0

    # Deterministic RSS proxy (MB)
    rss_mb_max = int(200 + load * 5)

    # Fill rate proxy: slightly lower under heavier load
    fill_rate = 0.6 if mode == "baseline" else 0.3

    return {
        "decision_latency_p99_ms": decision_latency_p99_ms,
        "order_latency_p99_ms": order_latency_p99_ms,
        "event_queue_depth_max": event_queue_depth_max,
        "snapshot_queue_depth_max": snapshot_queue_depth_max,
        "errors_total": errors_total,
        "events_dropped": events_dropped,
        "rss_mb_max": rss_mb_max,
        "fill_rate": fill_rate,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Run a synthetic soak test and write a JSON summary")
    p.add_argument("--symbols", type=str, required=True, help="Comma-separated symbols")
    p.add_argument("--duration-s", type=int, required=True, help="Soak duration in seconds")
    p.add_argument("--cadence-ms", type=int, required=True, help="Event cadence in milliseconds")
    p.add_argument("--mode", choices=["baseline", "overload"], required=True)
    p.add_argument("--output", type=Path, required=True, help="Output JSON path")
    # accepted for forward-compat; currently unused
    p.add_argument("--slow-consumer-lag-ms", type=int, default=0)
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    metrics = _compute_metrics(len(symbols), args.cadence_ms, args.mode)
    # add context for debugging/traceability
    payload = {
        **metrics,
        "mode": args.mode,
        "duration_s": args.duration_s,
        "cadence_ms": args.cadence_ms,
        "symbols_count": len(symbols),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
