"""Paper trading CLI wrapper.

Modes:
- Fixture mode: `grinder-paper --fixture <path>` runs paper trading on fixture data
- Live mode: `grinder-paper --live` runs health/metrics server (skeleton)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_fixture_config(fixture_dir: Path) -> dict[str, object]:
    """Load fixture config.json if it exists."""
    config_path = fixture_dir / "config.json"
    if config_path.exists():
        with config_path.open() as f:
            result: dict[str, object] = json.load(f)
            return result
    return {}


def _run_fixture_mode(args: argparse.Namespace) -> None:
    """Run paper trading on fixture data."""
    from grinder.paper import PaperEngine  # noqa: PLC0415 - lazy import

    fixture_dir = Path(args.fixture)

    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}", file=sys.stderr)
        raise SystemExit(1)

    config = _load_fixture_config(fixture_dir)
    controller_enabled = bool(config.get("controller_enabled", False))

    if args.verbose:
        print(f"Loading fixture from: {fixture_dir}")
        print("Paper trading mode: NO REAL ORDERS")
        if controller_enabled:
            print("Controller: ENABLED")

    engine = PaperEngine(controller_enabled=controller_enabled)
    result = engine.run(fixture_dir)

    if args.verbose:
        print(f"Events processed: {result.events_processed}")
        print(f"Events gated: {result.events_gated}")
        print(f"Orders placed (simulated): {result.orders_placed}")
        print(f"Orders blocked: {result.orders_blocked}")
        if result.errors:
            print(f"Errors: {len(result.errors)}")
            for err in result.errors:
                print(f"  - {err}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            json.dump(result.to_dict(), f, indent=2)
        if args.verbose:
            print(f"Output written to: {out_path}")

    print(f"Paper trading completed. Events processed: {result.events_processed}")
    print(f"Output digest: {result.digest}")


def _run_live_mode(args: argparse.Namespace) -> None:
    """Run live skeleton (health/metrics server)."""
    from scripts.run_live import main as live_main  # noqa: PLC0415

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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="grinder-paper",
        description="GRINDER paper trading (no real orders)",
    )

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--fixture",
        type=str,
        help="Run paper trading on fixture data (deterministic)",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Run live skeleton (health/metrics server)",
    )

    # Fixture mode options
    parser.add_argument("--out", help="Output path for paper trading JSON (optional)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # Live mode options
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="Comma-separated symbols")
    parser.add_argument("--duration-s", type=int, default=60, help="Duration seconds")
    parser.add_argument(
        "--metrics-port", type=int, default=9090, help="Port for /healthz and /metrics"
    )

    args = parser.parse_args()

    if args.fixture:
        _run_fixture_mode(args)
    elif args.live:
        _run_live_mode(args)
