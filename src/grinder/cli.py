"""Project CLI entrypoint.

This is intentionally minimal for the current skeleton stage.
The CLI delegates to scripts/* so packaging/UX is consistent.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grinder", description="GRINDER CLI")
    parser.add_argument("--version", action="version", version=f"grinder {_pkg_version()}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_live = sub.add_parser("live", help="Run live loop (currently a skeleton health/metrics server)")
    p_live.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="Comma-separated symbols")
    p_live.add_argument("--duration-s", type=int, default=0, help="Duration seconds (0 = forever)")
    p_live.add_argument("--metrics-port", type=int, default=9090, help="Port for /healthz and /metrics")

    p_replay = sub.add_parser("replay", help="Run deterministic replay on fixtures")
    p_replay.add_argument("--fixture", required=True, help="Path to fixture JSON")
    p_replay.add_argument("--out", required=True, help="Output path for replay JSON")

    sub.add_parser("verify-replay", help="Verify replay determinism (runs twice and compares digests)")

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
        _run_script("scripts.run_live", [
            "--symbols",
            args.symbols,
            "--duration-s",
            str(args.duration_s),
            "--metrics-port",
            str(args.metrics_port),
        ])
        return

    if args.cmd == "replay":
        _run_script("scripts.run_replay", ["--fixture", args.fixture, "--out", args.out])
        return

    if args.cmd == "verify-replay":
        _run_script("scripts.verify_replay_determinism", [])
        return

    if args.cmd == "secret-guard":
        argv = ["--verbose"] if args.verbose else []
        _run_script("scripts.secret_guard", argv)
        return

    raise SystemExit(2)
