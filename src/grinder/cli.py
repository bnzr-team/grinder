"""Project CLI entrypoint.

Provides CLI commands for GRINDER:
- grinder replay: End-to-end deterministic replay on fixtures
- grinder paper: Paper trading with gating (no real orders)
- grinder live: Run live loop (skeleton)
- grinder verify-replay: Verify replay determinism
- grinder secret-guard: Scan for secrets
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _pkg_version() -> str:
    try:
        return version("grinder")
    except PackageNotFoundError:
        return "0.0.0"


def _run_script(module: str, argv: list[str]) -> int:
    """Import a script module and call its main() with argv."""
    # Import inside to keep import-time side effects minimal.
    mod = __import__(module, fromlist=["main"])
    main = getattr(mod, "main", None)
    if main is None:
        print(f"ERROR: {module} has no main()", file=sys.stderr)
        return 2

    old_argv = sys.argv
    try:
        sys.argv = [module, *argv]
        main()
    finally:
        sys.argv = old_argv
    return 0


def _cmd_replay(args: argparse.Namespace) -> None:
    """Run end-to-end replay command."""
    from grinder.replay import ReplayEngine  # noqa: PLC0415 - lazy import for fast CLI startup

    fixture_dir = Path(args.fixture)

    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}", file=sys.stderr)
        raise SystemExit(1)

    if args.verbose:
        print(f"Loading fixture from: {fixture_dir}")

    engine = ReplayEngine()
    result = engine.run(fixture_dir)

    if args.verbose:
        print(f"Events processed: {result.events_processed}")
        print(f"Outputs generated: {len(result.outputs)}")
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

    print(f"Replay completed. Events processed: {result.events_processed}")
    print(f"Output digest: {result.digest}")


def _cmd_paper(args: argparse.Namespace) -> None:
    """Run paper trading command."""
    from grinder.paper import PaperEngine  # noqa: PLC0415 - lazy import for fast CLI startup

    fixture_dir = Path(args.fixture)

    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}", file=sys.stderr)
        raise SystemExit(1)

    if args.verbose:
        print(f"Loading fixture from: {fixture_dir}")
        print("Paper trading mode: NO REAL ORDERS")

    engine = PaperEngine()
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grinder", description="GRINDER CLI")
    parser.add_argument("--version", action="version", version=f"grinder {_pkg_version()}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_live = sub.add_parser(
        "live", help="Run live loop (currently a skeleton health/metrics server)"
    )
    p_live.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="Comma-separated symbols")
    p_live.add_argument("--duration-s", type=int, default=0, help="Duration seconds (0 = forever)")
    p_live.add_argument(
        "--metrics-port", type=int, default=9090, help="Port for /healthz and /metrics"
    )

    p_replay = sub.add_parser("replay", help="Run end-to-end deterministic replay on fixtures")
    p_replay.add_argument("--fixture", required=True, help="Path to fixture directory")
    p_replay.add_argument("--out", help="Output path for replay JSON (optional)")
    p_replay.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    p_paper = sub.add_parser("paper", help="Run paper trading with gating (no real orders)")
    p_paper.add_argument("--fixture", required=True, help="Path to fixture directory")
    p_paper.add_argument("--out", help="Output path for paper trading JSON (optional)")
    p_paper.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    sub.add_parser(
        "verify-replay", help="Verify replay determinism (runs twice and compares digests)"
    )

    p_secret = sub.add_parser("secret-guard", help="Scan repository for accidental secrets")
    p_secret.add_argument("--verbose", action="store_true")

    return parser


def main() -> None:
    parser = build_parser()
    args, unknown = parser.parse_known_args()

    if unknown:
        print(f"Unknown args: {unknown}", file=sys.stderr)
        raise SystemExit(2)

    if args.cmd == "live":
        _run_script(
            "scripts.run_live",
            [
                "--symbols",
                args.symbols,
                "--duration-s",
                str(args.duration_s),
                "--metrics-port",
                str(args.metrics_port),
            ],
        )
        return

    if args.cmd == "replay":
        _cmd_replay(args)
        return

    if args.cmd == "paper":
        _cmd_paper(args)
        return

    if args.cmd == "verify-replay":
        _run_script("scripts.verify_replay_determinism", [])
        return

    if args.cmd == "secret-guard":
        argv = ["--verbose"] if args.verbose else []
        _run_script("scripts.secret_guard", argv)
        return

    raise SystemExit(2)
