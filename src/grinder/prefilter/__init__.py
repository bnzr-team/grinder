"""Prefilter module for symbol gating.

See: docs/04_PREFILTER_SPEC.md
"""

from grinder.prefilter.constants import (
    OI_MIN_USD,
    SPREAD_MAX_BPS,
    TRADE_COUNT_MIN_1M,
    VOL_MIN_1H_USD,
    VOL_MIN_24H_USD,
)
from grinder.prefilter.gate import FilterResult, hard_filter

__all__ = [
    "OI_MIN_USD",
    "SPREAD_MAX_BPS",
    "TRADE_COUNT_MIN_1M",
    "VOL_MIN_1H_USD",
    "VOL_MIN_24H_USD",
    "FilterResult",
    "hard_filter",
]
