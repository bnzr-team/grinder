"""Toxicity gate for detecting adverse market conditions.

Toxicity v0 is rule-based and deterministic, detecting:
1. Spread spikes - abnormally wide spreads indicate market stress
2. Price impact - rapid price movement indicates toxic flow

See: docs/06_TOXICITY_SPEC.md for full specification (Planned)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal  # noqa: TC003 - used at runtime

from grinder.gating.types import GateReason, GatingResult


@dataclass
class ToxicityGate:
    """Toxicity gate for detecting adverse market conditions.

    v0 implementation uses rule-based detection:
    - Spread spike: blocks when spread_bps exceeds threshold
    - Price impact: blocks when price moves too fast (recent vs current)

    All detection is deterministic given the same inputs.

    Attributes:
        max_spread_bps: Maximum allowed spread in basis points.
        max_price_impact_bps: Maximum allowed price change in basis points
            over the lookback window.
        lookback_window_ms: Time window for price impact calculation.
    """

    max_spread_bps: float = 50.0
    max_price_impact_bps: float = 500.0  # 5% - high enough to avoid triggering on normal volatility
    lookback_window_ms: int = 5000

    # Internal state for price tracking (per-symbol)
    _price_history: dict[str, deque[tuple[int, Decimal]]] = field(default_factory=dict, repr=False)

    def _get_history(self, symbol: str) -> deque[tuple[int, Decimal]]:
        """Get or create price history for symbol."""
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=100)
        return self._price_history[symbol]

    def check(
        self,
        ts: int,
        symbol: str,
        spread_bps: float,
        mid_price: Decimal,
    ) -> GatingResult:
        """Check if market conditions are toxic.

        Args:
            ts: Current timestamp in milliseconds.
            symbol: Trading symbol.
            spread_bps: Current spread in basis points.
            mid_price: Current mid price.

        Returns:
            GatingResult indicating whether trading is allowed.
        """
        # Check 1: Spread spike
        if spread_bps > self.max_spread_bps:
            return GatingResult.block(
                GateReason.SPREAD_SPIKE,
                {
                    "spread_bps": spread_bps,
                    "max_spread_bps": self.max_spread_bps,
                    "excess_bps": spread_bps - self.max_spread_bps,
                },
            )

        # Check 2: Price impact (compare to oldest price in window for this symbol)
        history = self._get_history(symbol)
        if history:
            # Clean up old entries outside lookback window
            window_start = ts - self.lookback_window_ms
            while history and history[0][0] < window_start:
                history.popleft()

            # Calculate price impact if we have history
            if history:
                oldest_ts, oldest_price = history[0]
                if oldest_price > 0:
                    price_change_bps = abs(float((mid_price - oldest_price) / oldest_price) * 10000)
                    if price_change_bps > self.max_price_impact_bps:
                        return GatingResult.block(
                            GateReason.PRICE_IMPACT_HIGH,
                            {
                                "price_change_bps": round(price_change_bps, 2),
                                "max_price_impact_bps": self.max_price_impact_bps,
                                "oldest_price": str(oldest_price),
                                "current_price": str(mid_price),
                                "window_ms": ts - oldest_ts,
                            },
                        )

        return GatingResult.allow(
            {
                "spread_bps": spread_bps,
                "max_spread_bps": self.max_spread_bps,
                "prices_in_window": len(history) if history else 0,
            }
        )

    def record_price(self, ts: int, symbol: str, mid_price: Decimal) -> None:
        """Record a price observation for price impact calculation.

        Call this for every snapshot to build price history.

        Args:
            ts: Timestamp in milliseconds.
            symbol: Trading symbol.
            mid_price: Mid price at this timestamp.
        """
        history = self._get_history(symbol)
        history.append((ts, mid_price))

    def reset(self) -> None:
        """Reset the toxicity gate state."""
        self._price_history.clear()

    def prices_in_window(self, symbol: str) -> int:
        """Current count of prices in the lookback window for a symbol."""
        if symbol not in self._price_history:
            return 0
        return len(self._price_history[symbol])
