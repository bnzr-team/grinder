"""Tests for gating module (rate limiter + risk gate).

Tests:
- RateLimiter: rate limit enforcement, cooldown, recording
- RiskGate: notional limits, daily loss limit, recording
- GatingResult: serialization, factory methods
"""

from __future__ import annotations

from decimal import Decimal

from grinder.gating import GateReason, GatingResult, RateLimiter, RiskGate


class TestGatingResult:
    """Test GatingResult data class."""

    def test_allow_factory(self) -> None:
        """Test allow factory method."""
        result = GatingResult.allow({"foo": "bar"})
        assert result.allowed is True
        assert result.reason == GateReason.PASS
        assert result.details == {"foo": "bar"}

    def test_block_factory(self) -> None:
        """Test block factory method."""
        result = GatingResult.block(
            GateReason.RATE_LIMIT_EXCEEDED,
            {"count": 100},
        )
        assert result.allowed is False
        assert result.reason == GateReason.RATE_LIMIT_EXCEEDED
        assert result.details == {"count": 100}

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        result = GatingResult.block(GateReason.COOLDOWN_ACTIVE, {"ms": 50})
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["reason"] == "COOLDOWN_ACTIVE"
        assert d["details"] == {"ms": 50}

    def test_from_dict_roundtrip(self) -> None:
        """Test deserialization roundtrip."""
        original = GatingResult.block(GateReason.MAX_NOTIONAL_EXCEEDED, {"limit": 5000})
        d = original.to_dict()
        restored = GatingResult.from_dict(d)
        assert restored.allowed == original.allowed
        assert restored.reason == original.reason
        assert restored.details == original.details


class TestRateLimiter:
    """Test RateLimiter."""

    def test_allow_within_limit(self) -> None:
        """Test that orders within limit are allowed."""
        limiter = RateLimiter(max_orders_per_minute=10, cooldown_ms=0)
        result = limiter.check(ts=1000)
        assert result.allowed is True
        assert result.reason == GateReason.PASS

    def test_block_cooldown(self) -> None:
        """Test that orders within cooldown are blocked."""
        limiter = RateLimiter(max_orders_per_minute=100, cooldown_ms=500)

        # First order
        result1 = limiter.check(ts=1000)
        assert result1.allowed is True
        limiter.record_order(ts=1000)

        # Second order too soon (only 100ms later)
        result2 = limiter.check(ts=1100)
        assert result2.allowed is False
        assert result2.reason == GateReason.COOLDOWN_ACTIVE
        assert result2.details is not None
        assert result2.details["remaining_ms"] == 400

    def test_allow_after_cooldown(self) -> None:
        """Test that orders are allowed after cooldown expires."""
        limiter = RateLimiter(max_orders_per_minute=100, cooldown_ms=500)

        limiter.record_order(ts=1000)

        # After cooldown
        result = limiter.check(ts=1600)
        assert result.allowed is True

    def test_block_rate_limit(self) -> None:
        """Test that orders exceeding rate limit are blocked."""
        limiter = RateLimiter(max_orders_per_minute=3, cooldown_ms=0)

        # Place 3 orders
        for i in range(3):
            ts = 1000 + i * 100
            result = limiter.check(ts)
            assert result.allowed is True
            limiter.record_order(ts)

        # 4th order should be blocked
        result = limiter.check(ts=1300)
        assert result.allowed is False
        assert result.reason == GateReason.RATE_LIMIT_EXCEEDED

    def test_rate_limit_sliding_window(self) -> None:
        """Test that rate limit uses sliding window."""
        limiter = RateLimiter(max_orders_per_minute=2, cooldown_ms=0)

        # Place 2 orders at t=0 and t=1000
        limiter.record_order(ts=0)
        limiter.record_order(ts=1000)

        # At t=30000, first order expires (30s elapsed)
        result = limiter.check(ts=30000)
        assert result.allowed is False  # Still 2 in window

        # At t=60001, first order is outside window
        result = limiter.check(ts=60001)
        assert result.allowed is True  # Only 1 in window now

    def test_reset(self) -> None:
        """Test reset clears state."""
        limiter = RateLimiter(max_orders_per_minute=1, cooldown_ms=1000)
        limiter.record_order(ts=1000)

        # Should be blocked
        result = limiter.check(ts=1100)
        assert result.allowed is False

        # Reset and try again
        limiter.reset()
        result = limiter.check(ts=1100)
        assert result.allowed is True

    def test_orders_in_window_property(self) -> None:
        """Test orders_in_window property."""
        limiter = RateLimiter(max_orders_per_minute=10, cooldown_ms=0)
        assert limiter.orders_in_window == 0

        limiter.record_order(ts=1000)
        limiter.record_order(ts=2000)
        assert limiter.orders_in_window == 2


class TestRiskGate:
    """Test RiskGate."""

    def test_allow_within_limits(self) -> None:
        """Test that orders within limits are allowed."""
        gate = RiskGate(
            max_notional_per_symbol=Decimal("5000"),
            max_notional_total=Decimal("20000"),
            daily_loss_limit=Decimal("500"),
        )
        result = gate.check_order("BTCUSDT", Decimal("1000"))
        assert result.allowed is True
        assert result.reason == GateReason.PASS

    def test_block_symbol_notional_exceeded(self) -> None:
        """Test blocking when per-symbol notional exceeded."""
        gate = RiskGate(
            max_notional_per_symbol=Decimal("1000"),
            max_notional_total=Decimal("20000"),
            daily_loss_limit=Decimal("500"),
        )

        # First order OK
        result1 = gate.check_order("BTCUSDT", Decimal("800"))
        assert result1.allowed is True
        gate.record_order("BTCUSDT", Decimal("800"))

        # Second order would exceed symbol limit
        result2 = gate.check_order("BTCUSDT", Decimal("300"))
        assert result2.allowed is False
        assert result2.reason == GateReason.MAX_NOTIONAL_EXCEEDED
        assert result2.details is not None
        assert result2.details["scope"] == "symbol"

    def test_block_total_notional_exceeded(self) -> None:
        """Test blocking when total notional exceeded."""
        gate = RiskGate(
            max_notional_per_symbol=Decimal("5000"),
            max_notional_total=Decimal("2000"),
            daily_loss_limit=Decimal("500"),
        )

        gate.record_order("BTCUSDT", Decimal("1500"))

        # This order would exceed total limit (1500 + 600 > 2000)
        result = gate.check_order("ETHUSDT", Decimal("600"))
        assert result.allowed is False
        assert result.reason == GateReason.MAX_NOTIONAL_EXCEEDED
        assert result.details is not None
        assert result.details["scope"] == "total"

    def test_block_daily_loss_limit(self) -> None:
        """Test blocking when daily loss limit exceeded."""
        gate = RiskGate(
            max_notional_per_symbol=Decimal("5000"),
            max_notional_total=Decimal("20000"),
            daily_loss_limit=Decimal("100"),
        )

        # Record a loss
        gate.record_fill("BTCUSDT", Decimal("-1000"), Decimal("-150"))

        # New order should be blocked due to loss
        result = gate.check_order("BTCUSDT", Decimal("100"))
        assert result.allowed is False
        assert result.reason == GateReason.DAILY_LOSS_LIMIT_EXCEEDED

    def test_unrealized_pnl_affects_loss_check(self) -> None:
        """Test that unrealized PnL is included in loss check."""
        gate = RiskGate(
            max_notional_per_symbol=Decimal("5000"),
            max_notional_total=Decimal("20000"),
            daily_loss_limit=Decimal("100"),
        )

        # Set unrealized loss
        gate.update_unrealized_pnl(Decimal("-150"))

        # New order should be blocked
        result = gate.check_order("BTCUSDT", Decimal("100"))
        assert result.allowed is False
        assert result.reason == GateReason.DAILY_LOSS_LIMIT_EXCEEDED

    def test_record_fill_reduces_notional(self) -> None:
        """Test that fills can reduce notional."""
        gate = RiskGate(max_notional_per_symbol=Decimal("1000"))

        gate.record_order("BTCUSDT", Decimal("800"))
        assert gate.get_symbol_notional("BTCUSDT") == Decimal("800")

        # Fill reduces position
        gate.record_fill("BTCUSDT", Decimal("-300"), Decimal("10"))
        assert gate.get_symbol_notional("BTCUSDT") == Decimal("500")

    def test_reset(self) -> None:
        """Test reset clears all state."""
        gate = RiskGate()
        gate.record_order("BTCUSDT", Decimal("1000"))
        gate.record_fill("BTCUSDT", Decimal("-500"), Decimal("-50"))
        gate.update_unrealized_pnl(Decimal("-25"))

        gate.reset()

        assert gate.total_notional == Decimal("0")
        assert gate.total_pnl == Decimal("0")
        assert gate.get_symbol_notional("BTCUSDT") == Decimal("0")

    def test_total_notional_property(self) -> None:
        """Test total_notional property."""
        gate = RiskGate()
        gate.record_order("BTCUSDT", Decimal("1000"))
        gate.record_order("ETHUSDT", Decimal("500"))

        assert gate.total_notional == Decimal("1500")

    def test_total_pnl_property(self) -> None:
        """Test total_pnl combines realized and unrealized."""
        gate = RiskGate()
        gate.record_fill("BTCUSDT", Decimal("-100"), Decimal("20"))
        gate.update_unrealized_pnl(Decimal("-5"))

        assert gate.total_pnl == Decimal("15")  # 20 - 5
