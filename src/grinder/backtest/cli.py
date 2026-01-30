"""Backtest CLI wrapper.

For the current skeleton stage, backtest maps to replay execution.
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="grinder-backtest", description="GRINDER backtest (skeleton)"
    )
    parser.add_argument("--fixture", required=True, help="Path to fixture JSON")
    parser.add_argument("--out", required=True, help="Output path for replay JSON")
    args = parser.parse_args()

    # Delegate to the existing replay runner.
    from scripts.run_replay import main as replay_main  # type: ignore  # noqa: PLC0415

    old_argv = sys.argv
    try:
        sys.argv = ["scripts.run_replay", "--fixture", args.fixture, "--out", args.out]
        replay_main()
    finally:
        sys.argv = old_argv
