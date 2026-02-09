"""Replay module for deterministic backtesting.

Provides end-to-end replay pipeline:
  fixture -> prefilter -> policy -> execution -> output

See: docs/11_BACKTEST_PROTOCOL.md
"""

from grinder.replay.engine import ReplayEngine, ReplayOutput, ReplayResult
from grinder.replay.l2_snapshot import (
    IMPACT_INSUFFICIENT_DEPTH_BPS,
    QTY_REF_BASELINE,
    BookLevel,
    L2ParseError,
    L2Snapshot,
    load_l2_fixtures,
    parse_l2_snapshot_line,
)

__all__ = [
    "IMPACT_INSUFFICIENT_DEPTH_BPS",
    "QTY_REF_BASELINE",
    "BookLevel",
    "L2ParseError",
    "L2Snapshot",
    "ReplayEngine",
    "ReplayOutput",
    "ReplayResult",
    "load_l2_fixtures",
    "parse_l2_snapshot_line",
]
