"""Hard filter gating logic for prefilter v0.

This module implements rule-based symbol gating as per docs/04_PREFILTER_SPEC.md §4.4.

Limitations v0:
- Only hard gates, no scoring/ranking/top-K
- No stability controls (T_ENTER, T_HOLD, hysteresis)
- No diversity filtering
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from grinder.prefilter.constants import (
    BLACKLIST,
    OI_MIN_USD,
    SPREAD_MAX_BPS,
    TRADE_COUNT_MIN_1M,
    VOL_MIN_1H_USD,
    VOL_MIN_24H_USD,
)


class FilterReason(Enum):
    """Reason codes for filter decisions."""

    PASS = "PASS"
    SPREAD_TOO_HIGH = "SPREAD_TOO_HIGH"
    VOL_24H_TOO_LOW = "VOL_24H_TOO_LOW"
    VOL_1H_TOO_LOW = "VOL_1H_TOO_LOW"
    ACTIVITY_TOO_LOW = "ACTIVITY_TOO_LOW"
    OI_TOO_LOW = "OI_TOO_LOW"
    BLACKLISTED = "BLACKLISTED"
    DELISTING = "DELISTING"


@dataclass(frozen=True)
class FilterResult:
    """Result of hard filter evaluation.

    Attributes:
        allowed: True if symbol passed all gates
        reason: Reason code for the decision
        symbol: Symbol that was evaluated
    """

    allowed: bool
    reason: FilterReason
    symbol: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "allowed": self.allowed,
            "reason": self.reason.value,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FilterResult:
        """Create from dict."""
        return cls(
            allowed=d["allowed"],
            reason=FilterReason(d["reason"]),
            symbol=d["symbol"],
        )


def _check_block_reason(  # noqa: PLR0911
    symbol: str,
    features: dict[str, Any],
    spread_max_bps: float,
    vol_min_24h_usd: float,
    vol_min_1h_usd: float,
    trade_count_min_1m: int,
    oi_min_usd: float,
    blacklist: frozenset[str],
) -> FilterReason | None:
    """Check all gates and return first block reason, or None if all pass.

    Note: This function intentionally has many return statements for clarity.
    Each gate is a distinct check with early return on failure.
    """
    # Check blacklist first
    if symbol in blacklist:
        return FilterReason.BLACKLISTED

    # Check delisting flag
    if features.get("is_delisting", False):
        return FilterReason.DELISTING

    # Check spread
    if features.get("spread_bps", 0.0) > spread_max_bps:
        return FilterReason.SPREAD_TOO_HIGH

    # Check 24h volume
    if features.get("vol_24h_usd", 0.0) < vol_min_24h_usd:
        return FilterReason.VOL_24H_TOO_LOW

    # Check 1h volume (optional - skip if not provided)
    vol_1h = features.get("vol_1h_usd")
    if vol_1h is not None and vol_1h < vol_min_1h_usd:
        return FilterReason.VOL_1H_TOO_LOW

    # Check trade activity (optional - skip if not provided)
    trade_count = features.get("trade_count_1m")
    if trade_count is not None and trade_count < trade_count_min_1m:
        return FilterReason.ACTIVITY_TOO_LOW

    # Check open interest (optional - skip if not provided)
    oi = features.get("oi_usd")
    if oi is not None and oi < oi_min_usd:
        return FilterReason.OI_TOO_LOW

    return None


def hard_filter(
    symbol: str,
    features: dict[str, Any],
    *,
    spread_max_bps: float = SPREAD_MAX_BPS,
    vol_min_24h_usd: float = VOL_MIN_24H_USD,
    vol_min_1h_usd: float = VOL_MIN_1H_USD,
    trade_count_min_1m: int = TRADE_COUNT_MIN_1M,
    oi_min_usd: float = OI_MIN_USD,
    blacklist: frozenset[str] = BLACKLIST,
) -> FilterResult:
    """Apply hard gates to determine if symbol should be traded.

    Args:
        symbol: Trading symbol (e.g., "BTCUSDT")
        features: Dict containing:
            - spread_bps: Current bid-ask spread in bps
            - vol_24h_usd: 24h volume in USD
            - vol_1h_usd: 1h volume in USD (optional)
            - trade_count_1m: Trades per minute (optional)
            - oi_usd: Open interest in USD (optional)
            - is_delisting: True if symbol is being delisted (optional)
        spread_max_bps: Max spread threshold (default from constants)
        vol_min_24h_usd: Min 24h volume threshold
        vol_min_1h_usd: Min 1h volume threshold
        trade_count_min_1m: Min trades per minute threshold
        oi_min_usd: Min open interest threshold
        blacklist: Set of blacklisted symbols

    Returns:
        FilterResult with allowed status and reason code

    Example:
        >>> features = {"spread_bps": 10.0, "vol_24h_usd": 50_000_000}
        >>> result = hard_filter("BTCUSDT", features)
        >>> result.allowed
        True
        >>> result.reason
        FilterReason.PASS
    """
    block_reason = _check_block_reason(
        symbol,
        features,
        spread_max_bps,
        vol_min_24h_usd,
        vol_min_1h_usd,
        trade_count_min_1m,
        oi_min_usd,
        blacklist,
    )

    if block_reason is not None:
        return FilterResult(allowed=False, reason=block_reason, symbol=symbol)

    return FilterResult(allowed=True, reason=FilterReason.PASS, symbol=symbol)
