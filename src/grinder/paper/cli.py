"""Paper trading CLI wrapper.

For the current skeleton stage, this runs the live health/metrics server for a bounded duration.
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="grinder-paper", description="GRINDER paper trading (skeleton)")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="Comma-separated symbols")
    parser.add_argument("--duration-s", type=int, default=60, help="Duration seconds")
    parser.add_argument("--metrics-port", type=int, default=9090, help="Port for /healthz and /metrics")
    args = parser.parse_args()

    from scripts.run_live import main as live_main  # type: ignore  # noqa: PLC0415

    old_argv = sys.argv
    try:
        sys.argv = [
            "scripts.run_live",
            "--symbols",
            args.symbols,
            "--duration-s",
            str(args.duration_s),
            "--metrics-port",
            str(args.metrics_port),
        ]
        live_main()
    finally:
        sys.argv = old_argv
