"""Unit tests for RateLimiter.

Tests verify:
- Cooldown enforcement between orders
- Max orders per minute sliding window
- Fake clock testing (timestamp-based, no system clock dependency)

See ADR-035 for BinanceExchangePort context.
"""

from __future__ import annotations

from grinder.gating import GateReason, RateLimiter


class TestRateLimiterCooldown:
    """Tests for cooldown between orders."""

    def test_first_order_allowed(self) -> None:
        """First order is always allowed."""
        limiter = RateLimiter(cooldown_ms=100)
        result = limiter.check(ts=1000)
        assert result.allowed is True

    def test_cooldown_blocks_immediate_order(self) -> None:
        """Order within cooldown period is blocked."""
        limiter = RateLimiter(cooldown_ms=100)

        # Place first order
        result1 = limiter.check(ts=1000)
        assert result1.allowed is True
        limiter.record_order(ts=1000)

        # Immediate second order is blocked
        result2 = limiter.check(ts=1050)  # 50ms later (< 100ms cooldown)
        assert result2.allowed is False
        assert result2.reason == GateReason.COOLDOWN_ACTIVE
        assert result2.details["remaining_ms"] == 50

    def test_cooldown_allows_after_period(self) -> None:
        """Order after cooldown period is allowed."""
        limiter = RateLimiter(cooldown_ms=100)

        # Place first order
        limiter.check(ts=1000)
        limiter.record_order(ts=1000)

        # Order after cooldown is allowed
        result = limiter.check(ts=1100)  # Exactly 100ms later
        assert result.allowed is True


class TestRateLimiterSlidingWindow:
    """Tests for max orders per minute sliding window."""

    def test_rate_limit_blocks_excess_orders(self) -> None:
        """Orders exceeding max_per_minute are blocked."""
        limiter = RateLimiter(max_orders_per_minute=3, cooldown_ms=0)

        # Place 3 orders (at max)
        for i in range(3):
            ts = 1000 + i * 10  # 10ms apart
            result = limiter.check(ts=ts)
            assert result.allowed is True
            limiter.record_order(ts=ts)

        # 4th order is blocked
        result = limiter.check(ts=1030)
        assert result.allowed is False
        assert result.reason == GateReason.RATE_LIMIT_EXCEEDED
        assert result.details["current_count"] == 3
        assert result.details["max_per_minute"] == 3

    def test_sliding_window_expires_old_orders(self) -> None:
        """Orders older than 60 seconds expire from window."""
        limiter = RateLimiter(max_orders_per_minute=2, cooldown_ms=0)

        # Place 2 orders at t=0
        limiter.record_order(ts=0)
        limiter.record_order(ts=10)

        # At t=59999, still blocked (window hasn't expired)
        result = limiter.check(ts=59_999)
        assert result.allowed is False

        # At t=60001, first order expired, one slot available
        result = limiter.check(ts=60_001)
        assert result.allowed is True
        assert result.details["current_count"] == 1  # Only ts=10 remains


class TestRateLimiterFakeClock:
    """Tests proving rate limiter works with fake clock (timestamp injection).

    CRITICAL: These tests demonstrate that RateLimiter is time-testable
    without mocking system time - it takes ts as a parameter.
    """

    def test_fake_clock_time_travel(self) -> None:
        """Demonstrate time travel with fake timestamps."""
        limiter = RateLimiter(max_orders_per_minute=10, cooldown_ms=1000)

        # t=1000: First order (use ts > 0 to avoid edge case)
        assert limiter.check(ts=1000).allowed is True
        limiter.record_order(ts=1000)

        # t=1500: Blocked by cooldown (fake clock shows 500ms elapsed)
        result = limiter.check(ts=1500)
        assert result.allowed is False
        assert result.reason == GateReason.COOLDOWN_ACTIVE

        # t=2000: Allowed (fake clock shows 1000ms elapsed since last order)
        assert limiter.check(ts=2000).allowed is True
        limiter.record_order(ts=2000)

        # t=62000: First order expired (fake clock shows 61s since t=1000)
        # Only t=2000 order in window
        result = limiter.check(ts=62_000)
        assert result.allowed is True
        assert result.details["current_count"] == 1

    def test_rapid_fire_with_zero_cooldown(self) -> None:
        """Test rate limiting without cooldown (pure window limit)."""
        limiter = RateLimiter(max_orders_per_minute=5, cooldown_ms=0)

        # Rapid fire 5 orders at same timestamp
        for _ in range(5):
            assert limiter.check(ts=1000).allowed is True
            limiter.record_order(ts=1000)

        # 6th order blocked
        result = limiter.check(ts=1000)
        assert result.allowed is False
        assert result.reason == GateReason.RATE_LIMIT_EXCEEDED


class TestRateLimiterReset:
    """Tests for reset functionality."""

    def test_reset_clears_all_state(self) -> None:
        """reset() clears order history and cooldown."""
        limiter = RateLimiter(max_orders_per_minute=1, cooldown_ms=1000)

        # Place order and hit limits
        limiter.record_order(ts=0)
        assert limiter.check(ts=500).allowed is False  # Cooldown
        assert limiter.check(ts=1000).allowed is False  # Rate limit

        # Reset clears all
        limiter.reset()

        # Now allowed
        assert limiter.check(ts=1000).allowed is True
        assert limiter.orders_in_window == 0
