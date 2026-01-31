"""Tests for gating module (rate limiter + risk gate + toxicity gate).

Tests:
- RateLimiter: rate limit enforcement, cooldown, recording
- RiskGate: notional limits, daily loss limit, recording
- ToxicityGate: spread spike, price impact detection
- GatingResult: serialization, factory methods
"""

from __future__ import annotations

from decimal import Decimal

from grinder.gating import GateReason, GatingResult, RateLimiter, RiskGate, ToxicityGate


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


class TestToxicityGate:
    """Test ToxicityGate."""

    def test_allow_within_spread_limit(self) -> None:
        """Test that normal spread is allowed."""
        gate = ToxicityGate(max_spread_bps=50.0)
        result = gate.check(ts=1000, symbol="BTCUSDT", spread_bps=30.0, mid_price=Decimal("100"))
        assert result.allowed is True
        assert result.reason == GateReason.PASS

    def test_block_spread_spike(self) -> None:
        """Test that spread exceeding threshold is blocked."""
        gate = ToxicityGate(max_spread_bps=50.0)
        result = gate.check(ts=1000, symbol="BTCUSDT", spread_bps=75.0, mid_price=Decimal("100"))
        assert result.allowed is False
        assert result.reason == GateReason.SPREAD_SPIKE
        assert result.details is not None
        assert result.details["spread_bps"] == 75.0
        assert result.details["max_spread_bps"] == 50.0
        assert result.details["excess_bps"] == 25.0

    def test_allow_within_price_impact_limit(self) -> None:
        """Test that small price moves are allowed."""
        gate = ToxicityGate(max_price_impact_bps=100.0, lookback_window_ms=5000)

        # Record initial price
        gate.record_price(ts=1000, symbol="BTCUSDT", mid_price=Decimal("100"))

        # Check with small price move (0.5% = 50 bps)
        result = gate.check(ts=2000, symbol="BTCUSDT", spread_bps=10.0, mid_price=Decimal("100.5"))
        assert result.allowed is True
        assert result.reason == GateReason.PASS

    def test_block_price_impact_high(self) -> None:
        """Test that large price moves are blocked."""
        gate = ToxicityGate(max_price_impact_bps=100.0, lookback_window_ms=5000)

        # Record initial price
        gate.record_price(ts=1000, symbol="BTCUSDT", mid_price=Decimal("100"))

        # Check with large price move (2% = 200 bps)
        result = gate.check(ts=2000, symbol="BTCUSDT", spread_bps=10.0, mid_price=Decimal("102"))
        assert result.allowed is False
        assert result.reason == GateReason.PRICE_IMPACT_HIGH
        assert result.details is not None
        assert result.details["price_change_bps"] == 200.0
        assert result.details["max_price_impact_bps"] == 100.0

    def test_price_impact_window_expiry(self) -> None:
        """Test that old prices outside window are ignored."""
        gate = ToxicityGate(max_price_impact_bps=100.0, lookback_window_ms=5000)

        # Record price at t=0
        gate.record_price(ts=0, symbol="BTCUSDT", mid_price=Decimal("100"))

        # At t=6000 (outside 5000ms window), old price should be ignored
        # Even with 10% price move, should pass because no history in window
        result = gate.check(ts=6000, symbol="BTCUSDT", spread_bps=10.0, mid_price=Decimal("110"))
        assert result.allowed is True
        assert result.reason == GateReason.PASS

    def test_reset_clears_price_history(self) -> None:
        """Test reset clears price history."""
        gate = ToxicityGate(max_price_impact_bps=100.0)

        gate.record_price(ts=1000, symbol="BTCUSDT", mid_price=Decimal("100"))
        assert gate.prices_in_window("BTCUSDT") == 1

        gate.reset()
        assert gate.prices_in_window("BTCUSDT") == 0

    def test_prices_in_window_method(self) -> None:
        """Test prices_in_window method."""
        gate = ToxicityGate()
        assert gate.prices_in_window("BTCUSDT") == 0

        gate.record_price(ts=1000, symbol="BTCUSDT", mid_price=Decimal("100"))
        gate.record_price(ts=2000, symbol="BTCUSDT", mid_price=Decimal("101"))
        assert gate.prices_in_window("BTCUSDT") == 2

    def test_spread_check_takes_priority(self) -> None:
        """Test that spread spike is checked before price impact."""
        gate = ToxicityGate(max_spread_bps=50.0, max_price_impact_bps=100.0)

        # Record price that would trigger price impact
        gate.record_price(ts=1000, symbol="BTCUSDT", mid_price=Decimal("100"))

        # Both spread and price impact exceed thresholds
        # Should get SPREAD_SPIKE (checked first)
        result = gate.check(ts=2000, symbol="BTCUSDT", spread_bps=100.0, mid_price=Decimal("110"))
        assert result.allowed is False
        assert result.reason == GateReason.SPREAD_SPIKE

    def test_per_symbol_price_history(self) -> None:
        """Test that price history is tracked per-symbol."""
        gate = ToxicityGate(max_price_impact_bps=100.0, lookback_window_ms=5000)

        # Record different prices for different symbols
        gate.record_price(ts=1000, symbol="BTCUSDT", mid_price=Decimal("50000"))
        gate.record_price(ts=1000, symbol="ETHUSDT", mid_price=Decimal("3000"))

        # Check ETHUSDT - should compare against ETH's history, not BTC's
        result = gate.check(ts=2000, symbol="ETHUSDT", spread_bps=10.0, mid_price=Decimal("3000"))
        assert result.allowed is True  # No price change for ETH

        # Large move in BTC should be detected
        result = gate.check(ts=2000, symbol="BTCUSDT", spread_bps=10.0, mid_price=Decimal("51000"))
        assert result.allowed is False
        assert result.reason == GateReason.PRICE_IMPACT_HIGH
