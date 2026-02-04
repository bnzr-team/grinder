"""Unit tests for PaperExecutionAdapter.

Tests cover:
- Order placement (deterministic order_id)
- Order cancellation (lifecycle rules)
- Order replacement (cancel+new pattern)
- Instant fill semantics (v0)
- Error handling
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.connectors import (
    OrderRequest,
    OrderType,
    PaperExecutionAdapter,
    PaperOrder,
    PaperOrderError,
)
from grinder.core import OrderSide, OrderState


class FakeClock:
    """Fake clock for deterministic timestamps."""

    def __init__(self, start_time: float = 0.0) -> None:
        self._time = start_time

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


# --- Deterministic Order ID Tests ---


class TestDeterministicOrderId:
    """Tests for deterministic order ID generation."""

    def test_order_id_is_sequential(self) -> None:
        """Order IDs should be sequential and predictable."""
        adapter = PaperExecutionAdapter(order_id_prefix="TEST")

        result1 = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )
        result2 = adapter.place_order(
            OrderRequest(
                symbol="ETHUSDT",
                side=OrderSide.SELL,
                price=Decimal("3000"),
                quantity=Decimal("10"),
            )
        )

        assert result1.order_id == "TEST_00000001"
        assert result2.order_id == "TEST_00000002"

    def test_order_id_with_custom_prefix(self) -> None:
        """Order IDs should use the configured prefix."""
        adapter = PaperExecutionAdapter(order_id_prefix="CUSTOM")

        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        assert result.order_id.startswith("CUSTOM_")

    def test_order_id_deterministic_across_reset(self) -> None:
        """After reset, order IDs should restart from 1."""
        adapter = PaperExecutionAdapter(order_id_prefix="TEST")

        adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )
        adapter.reset()

        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        assert result.order_id == "TEST_00000001"


# --- Place Order Tests ---


class TestPlaceOrder:
    """Tests for order placement."""

    def test_place_order_instant_fill(self) -> None:
        """V0: Orders should be instantly filled."""
        adapter = PaperExecutionAdapter()

        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1.5"),
            )
        )

        assert result.state == OrderState.FILLED
        assert result.filled_quantity == Decimal("1.5")
        assert result.quantity == Decimal("1.5")

    def test_place_order_preserves_details(self) -> None:
        """Order result should preserve request details."""
        adapter = PaperExecutionAdapter()

        result = adapter.place_order(
            OrderRequest(
                symbol="ETHUSDT",
                side=OrderSide.SELL,
                price=Decimal("3000.50"),
                quantity=Decimal("10"),
                client_order_id="my_order_123",
            )
        )

        assert result.symbol == "ETHUSDT"
        assert result.side == OrderSide.SELL
        assert result.price == Decimal("3000.50")
        assert result.quantity == Decimal("10")

    def test_place_order_invalid_quantity(self) -> None:
        """Should reject orders with invalid quantity."""
        adapter = PaperExecutionAdapter()

        with pytest.raises(PaperOrderError, match="Invalid quantity"):
            adapter.place_order(
                OrderRequest(
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("50000"),
                    quantity=Decimal("0"),
                )
            )

    def test_place_order_invalid_price(self) -> None:
        """Should reject limit orders with invalid price."""
        adapter = PaperExecutionAdapter()

        with pytest.raises(PaperOrderError, match="Invalid price"):
            adapter.place_order(
                OrderRequest(
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("-100"),
                    quantity=Decimal("1"),
                    order_type=OrderType.LIMIT,
                )
            )

    def test_place_order_updates_stats(self) -> None:
        """Placing orders should update statistics."""
        adapter = PaperExecutionAdapter()

        adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        assert adapter.stats.orders_placed == 1
        assert adapter.stats.orders_filled == 1

    def test_place_order_uses_clock_for_timestamp(self) -> None:
        """Should use injected clock for timestamps."""
        clock = FakeClock(start_time=1000.0)
        adapter = PaperExecutionAdapter(clock=clock)

        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        assert result.created_ts == 1000000  # 1000.0 * 1000


# --- Cancel Order Tests ---


class TestCancelOrder:
    """Tests for order cancellation."""

    def test_cancel_order_not_found(self) -> None:
        """Should raise error for non-existent order."""
        adapter = PaperExecutionAdapter()

        with pytest.raises(PaperOrderError, match="Order not found"):
            adapter.cancel_order("NONEXISTENT")

    def test_cancel_order_already_cancelled_is_idempotent(self) -> None:
        """Cancelling an already cancelled order should be idempotent."""
        adapter = PaperExecutionAdapter()

        # V0: Orders are instantly filled, so we can't normally cancel them.
        # But we can test the idempotent cancel path by manually setting state.
        # For this test, we need to place and then check the cancel path.
        # Since V0 instantly fills, we'll test via the replace path which creates
        # cancelled orders.

        # Actually, let's test this by manipulating internal state

        adapter._orders["TEST_001"] = PaperOrder(
            order_id="TEST_001",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            state=OrderState.CANCELLED,
            order_type=OrderType.LIMIT,
            created_ts=1000,
            updated_ts=1000,
        )

        # Should return without error (idempotent)
        result = adapter.cancel_order("TEST_001")
        assert result.state == OrderState.CANCELLED

    def test_cancel_filled_order_raises_error(self) -> None:
        """Should raise error when trying to cancel filled order."""
        adapter = PaperExecutionAdapter()

        # Place order (instantly fills in v0)
        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        # Try to cancel filled order
        with pytest.raises(PaperOrderError, match="Cannot cancel filled order"):
            adapter.cancel_order(result.order_id)

    def test_cancel_order_updates_stats(self) -> None:
        """Cancelling orders should update statistics."""
        adapter = PaperExecutionAdapter()

        # Create a pending order manually

        adapter._orders["TEST_001"] = PaperOrder(
            order_id="TEST_001",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            state=OrderState.PENDING,
            order_type=OrderType.LIMIT,
            created_ts=1000,
            updated_ts=1000,
        )

        adapter.cancel_order("TEST_001")

        assert adapter.stats.orders_cancelled == 1


# --- Replace Order Tests ---


class TestReplaceOrder:
    """Tests for order replacement."""

    def test_replace_order_not_found(self) -> None:
        """Should raise error for non-existent order."""
        adapter = PaperExecutionAdapter()

        with pytest.raises(PaperOrderError, match="Order not found"):
            adapter.replace_order("NONEXISTENT", new_price=Decimal("60000"))

    def test_replace_filled_order_raises_error(self) -> None:
        """Should raise error when trying to replace filled order."""
        adapter = PaperExecutionAdapter()

        # Place order (instantly fills in v0)
        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        # Try to replace filled order
        with pytest.raises(PaperOrderError, match="Cannot replace order in state"):
            adapter.replace_order(result.order_id, new_price=Decimal("60000"))

    def test_replace_order_creates_new_order(self) -> None:
        """Replace should cancel old and create new order."""
        adapter = PaperExecutionAdapter()

        # Create a pending order manually

        adapter._orders["TEST_001"] = PaperOrder(
            order_id="TEST_001",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            state=OrderState.PENDING,
            order_type=OrderType.LIMIT,
            created_ts=1000,
            updated_ts=1000,
        )
        adapter._seq = 1  # So next order ID will be 2

        result = adapter.replace_order("TEST_001", new_price=Decimal("55000"))

        # Old order should be cancelled
        old_order = adapter.get_order("TEST_001")
        assert old_order is not None
        assert old_order.state == OrderState.CANCELLED

        # New order should be created with new price
        assert result.order_id == "PAPER_00000002"
        assert result.price == Decimal("55000")
        assert result.state == OrderState.FILLED  # V0 instant fill

    def test_replace_order_preserves_unchanged_fields(self) -> None:
        """Replace should keep fields that weren't changed."""
        adapter = PaperExecutionAdapter()

        # Create a pending order manually

        adapter._orders["TEST_001"] = PaperOrder(
            order_id="TEST_001",
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            price=Decimal("3000"),
            quantity=Decimal("10"),
            filled_quantity=Decimal("0"),
            state=OrderState.PENDING,
            order_type=OrderType.LIMIT,
            created_ts=1000,
            updated_ts=1000,
            client_order_id="my_client_id",
        )
        adapter._seq = 1

        # Only change price, not quantity
        result = adapter.replace_order("TEST_001", new_price=Decimal("3100"))

        assert result.symbol == "ETHUSDT"
        assert result.side == OrderSide.SELL
        assert result.quantity == Decimal("10")  # Unchanged

    def test_replace_order_updates_stats(self) -> None:
        """Replacing orders should update statistics."""
        adapter = PaperExecutionAdapter()

        # Create a pending order manually

        adapter._orders["TEST_001"] = PaperOrder(
            order_id="TEST_001",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            state=OrderState.PENDING,
            order_type=OrderType.LIMIT,
            created_ts=1000,
            updated_ts=1000,
        )

        adapter.replace_order("TEST_001", new_price=Decimal("55000"))

        assert adapter.stats.orders_replaced == 1
        assert adapter.stats.orders_placed == 1  # New order was placed
        assert adapter.stats.orders_filled == 1  # New order was filled (v0)


# --- Reset Tests ---


class TestReset:
    """Tests for adapter reset."""

    def test_reset_clears_orders(self) -> None:
        """Reset should clear all orders."""
        adapter = PaperExecutionAdapter()

        adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        adapter.reset()

        assert len(adapter.orders) == 0

    def test_reset_clears_stats(self) -> None:
        """Reset should clear statistics."""
        adapter = PaperExecutionAdapter()

        adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        adapter.reset()

        assert adapter.stats.orders_placed == 0
        assert adapter.stats.orders_filled == 0

    def test_reset_resets_sequence(self) -> None:
        """Reset should reset the sequence counter."""
        adapter = PaperExecutionAdapter(order_id_prefix="TEST")

        adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        adapter.reset()

        result = adapter.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )
        )

        assert result.order_id == "TEST_00000001"
