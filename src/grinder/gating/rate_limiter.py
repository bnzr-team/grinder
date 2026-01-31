"""Rate limiter for order throttling.

Implements order rate limiting based on:
- Max orders per minute (sliding window)
- Cooldown between orders (minimum interval)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from grinder.gating.types import GateReason, GatingResult


@dataclass
class RateLimiter:
    """Rate limiter for order throttling.

    Attributes:
        max_orders_per_minute: Maximum orders allowed in a 60-second window.
        cooldown_ms: Minimum milliseconds between consecutive orders.
    """

    max_orders_per_minute: int = 60
    cooldown_ms: int = 100

    # Internal state (not frozen - mutable)
    _order_timestamps: deque[int] = field(default_factory=deque, repr=False)
    _last_order_ts: int = field(default=0, repr=False)

    def check(self, ts: int) -> GatingResult:
        """Check if an order is allowed at the given timestamp.

        Args:
            ts: Current timestamp in milliseconds.

        Returns:
            GatingResult indicating whether the order is allowed.
        """
        # Check cooldown
        if self._last_order_ts > 0:
            elapsed = ts - self._last_order_ts
            if elapsed < self.cooldown_ms:
                return GatingResult.block(
                    GateReason.COOLDOWN_ACTIVE,
                    {
                        "elapsed_ms": elapsed,
                        "cooldown_ms": self.cooldown_ms,
                        "remaining_ms": self.cooldown_ms - elapsed,
                    },
                )

        # Purge old timestamps (older than 60 seconds)
        window_start = ts - 60_000
        while self._order_timestamps and self._order_timestamps[0] < window_start:
            self._order_timestamps.popleft()

        # Check rate limit
        current_count = len(self._order_timestamps)
        if current_count >= self.max_orders_per_minute:
            return GatingResult.block(
                GateReason.RATE_LIMIT_EXCEEDED,
                {
                    "current_count": current_count,
                    "max_per_minute": self.max_orders_per_minute,
                    "window_start_ts": window_start,
                },
            )

        return GatingResult.allow(
            {
                "current_count": current_count,
                "max_per_minute": self.max_orders_per_minute,
            }
        )

    def record_order(self, ts: int) -> None:
        """Record that an order was placed at the given timestamp.

        Call this after successfully placing an order.

        Args:
            ts: Timestamp when the order was placed.
        """
        self._order_timestamps.append(ts)
        self._last_order_ts = ts

    def reset(self) -> None:
        """Reset the rate limiter state."""
        self._order_timestamps.clear()
        self._last_order_ts = 0

    @property
    def orders_in_window(self) -> int:
        """Current count of orders in the sliding window."""
        return len(self._order_timestamps)
