"""Tests for paper trading fill simulation.

Tests:
- simulate_fills crossing/touch model (v1)
- simulate_fills instant mode (v0 backward compat)
- Determinism: same inputs → same fills
- Fill data class serialization
"""

from __future__ import annotations

from decimal import Decimal

from grinder.paper.fills import Fill, simulate_fills


class TestFillDataClass:
    """Test Fill data class."""

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="test_order_1",
        )
        d = fill.to_dict()
        assert d["ts"] == 1000
        assert d["symbol"] == "BTCUSDT"
        assert d["side"] == "BUY"
        assert d["price"] == "50000"
        assert d["quantity"] == "0.1"
        assert d["order_id"] == "test_order_1"

    def test_from_dict(self) -> None:
        """Test deserialization from dict."""
        d = {
            "ts": 2000,
            "symbol": "ETHUSDT",
            "side": "SELL",
            "price": "3000.50",
            "quantity": "1.5",
            "order_id": "order_xyz",
        }
        fill = Fill.from_dict(d)
        assert fill.ts == 2000
        assert fill.symbol == "ETHUSDT"
        assert fill.side == "SELL"
        assert fill.price == Decimal("3000.50")
        assert fill.quantity == Decimal("1.5")
        assert fill.order_id == "order_xyz"

    def test_roundtrip(self) -> None:
        """Test serialization roundtrip."""
        fill = Fill(
            ts=3000,
            symbol="SOLUSDT",
            side="BUY",
            price=Decimal("123.456"),
            quantity=Decimal("10"),
            order_id="roundtrip_1",
        )
        fill2 = Fill.from_dict(fill.to_dict())
        assert fill == fill2


class TestCrossingTouchFills:
    """Test v1 crossing/touch fill model.

    BUY fills if mid_price <= limit_price (price came down to our buy level)
    SELL fills if mid_price >= limit_price (price came up to our sell level)
    """

    def test_buy_crosses_fills(self) -> None:
        """BUY order fills when mid_price <= limit_price."""
        actions = [
            {
                "action_type": "PLACE",
                "side": "BUY",
                "price": "50000",
                "quantity": "1",
            }
        ]
        # Mid price at or below limit → fills
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        assert len(fills) == 1
        assert fills[0].side == "BUY"
        assert fills[0].price == Decimal("50000")

        # Mid price below limit → also fills
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("49900"))
        assert len(fills) == 1

    def test_buy_no_cross_no_fill(self) -> None:
        """BUY order does NOT fill when mid_price > limit_price."""
        actions = [
            {
                "action_type": "PLACE",
                "side": "BUY",
                "price": "49000",
                "quantity": "1",
            }
        ]
        # Mid price above limit → no fill (price hasn't reached our buy level)
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        assert len(fills) == 0

    def test_sell_crosses_fills(self) -> None:
        """SELL order fills when mid_price >= limit_price."""
        actions = [
            {
                "action_type": "PLACE",
                "side": "SELL",
                "price": "51000",
                "quantity": "1",
            }
        ]
        # Mid price at or above limit → fills
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("51000"))
        assert len(fills) == 1
        assert fills[0].side == "SELL"
        assert fills[0].price == Decimal("51000")

        # Mid price above limit → also fills
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("52000"))
        assert len(fills) == 1

    def test_sell_no_cross_no_fill(self) -> None:
        """SELL order does NOT fill when mid_price < limit_price."""
        actions = [
            {
                "action_type": "PLACE",
                "side": "SELL",
                "price": "52000",
                "quantity": "1",
            }
        ]
        # Mid price below limit → no fill (price hasn't reached our sell level)
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        assert len(fills) == 0

    def test_mixed_orders_partial_fill(self) -> None:
        """Multiple orders: only those that cross fill."""
        actions = [
            # BUY at 49000 - won't fill (mid 50000 > 49000)
            {"action_type": "PLACE", "side": "BUY", "price": "49000", "quantity": "1"},
            # BUY at 50000 - will fill (mid 50000 <= 50000)
            {"action_type": "PLACE", "side": "BUY", "price": "50000", "quantity": "1"},
            # SELL at 51000 - won't fill (mid 50000 < 51000)
            {"action_type": "PLACE", "side": "SELL", "price": "51000", "quantity": "1"},
            # SELL at 50000 - will fill (mid 50000 >= 50000)
            {"action_type": "PLACE", "side": "SELL", "price": "50000", "quantity": "1"},
        ]
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        assert len(fills) == 2
        # Check we got the right fills
        sides = {f.side for f in fills}
        assert sides == {"BUY", "SELL"}
        prices = {f.price for f in fills}
        assert prices == {Decimal("50000")}

    def test_cancel_actions_never_fill(self) -> None:
        """CANCEL actions never generate fills."""
        actions = [
            {"action_type": "CANCEL", "order_id": "some_order"},
            {"action_type": "PLACE", "side": "BUY", "price": "50000", "quantity": "1"},
        ]
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        assert len(fills) == 1  # Only the PLACE fills


class TestInstantModeFills:
    """Test v0 instant fill mode (backward compatibility)."""

    def test_instant_mode_all_orders_fill(self) -> None:
        """In instant mode, all PLACE orders fill regardless of mid_price."""
        actions = [
            # BUY far below mid - would NOT fill in crossing mode
            {"action_type": "PLACE", "side": "BUY", "price": "40000", "quantity": "1"},
            # SELL far above mid - would NOT fill in crossing mode
            {"action_type": "PLACE", "side": "SELL", "price": "60000", "quantity": "1"},
        ]
        # In instant mode, both fill
        fills = simulate_fills(
            1000, "BTCUSDT", actions, mid_price=Decimal("50000"), fill_mode="instant"
        )
        assert len(fills) == 2

    def test_instant_mode_no_mid_price_required(self) -> None:
        """Instant mode works even without mid_price."""
        actions = [
            {"action_type": "PLACE", "side": "BUY", "price": "50000", "quantity": "1"},
        ]
        # mid_price=None with instant mode
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=None, fill_mode="instant")
        assert len(fills) == 1


class TestFillDeterminism:
    """Test fill simulation determinism."""

    def test_same_inputs_same_fills(self) -> None:
        """Identical inputs produce identical fills."""
        actions = [
            {"action_type": "PLACE", "side": "BUY", "price": "49500", "quantity": "0.5"},
            {"action_type": "PLACE", "side": "SELL", "price": "50500", "quantity": "0.5"},
        ]
        mid = Decimal("50000")

        fills1 = simulate_fills(1000, "BTCUSDT", actions, mid_price=mid)
        fills2 = simulate_fills(1000, "BTCUSDT", actions, mid_price=mid)

        assert len(fills1) == len(fills2)
        for f1, f2 in zip(fills1, fills2, strict=True):
            assert f1 == f2

    def test_order_id_deterministic(self) -> None:
        """Generated order IDs are deterministic."""
        actions = [
            {"action_type": "PLACE", "side": "BUY", "price": "50000", "quantity": "1"},
        ]
        fills1 = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        fills2 = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))

        assert fills1[0].order_id == fills2[0].order_id
        assert fills1[0].order_id == "paper_1000_BTCUSDT_0_BUY_50000"

    def test_fill_preserves_action_order(self) -> None:
        """Fills maintain order relative to actions."""
        actions = [
            {"action_type": "PLACE", "side": "BUY", "price": "50000", "quantity": "1"},
            {"action_type": "PLACE", "side": "BUY", "price": "50100", "quantity": "2"},
            {"action_type": "PLACE", "side": "SELL", "price": "49900", "quantity": "3"},
        ]
        # All should fill with mid at 50000
        fills = simulate_fills(1000, "BTCUSDT", actions, mid_price=Decimal("50000"))
        assert len(fills) == 3
        assert fills[0].quantity == Decimal("1")
        assert fills[1].quantity == Decimal("2")
        assert fills[2].quantity == Decimal("3")
