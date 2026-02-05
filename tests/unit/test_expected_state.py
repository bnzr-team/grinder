"""Tests for ExpectedStateStore."""

from decimal import Decimal

from grinder.core import OrderSide, OrderState
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.types import ExpectedOrder, ExpectedPosition


class TestExpectedStateStore:
    """Tests for ExpectedStateStore."""

    def test_record_order(self) -> None:
        """Test recording an order."""
        store = ExpectedStateStore()
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,
        )

        store.record_order(order)

        assert store.order_count == 1
        assert store.get_order("grinder_BTCUSDT_1_1000000_1") == order

    def test_get_order_returns_none_for_missing(self) -> None:
        """Test get_order returns None for non-existent order."""
        store = ExpectedStateStore()

        result = store.get_order("nonexistent")

        assert result is None

    def test_mark_filled(self) -> None:
        """Test marking an order as filled."""
        store = ExpectedStateStore()
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,
        )
        store.record_order(order)

        store.mark_filled("grinder_BTCUSDT_1_1000000_1")

        updated = store.get_order("grinder_BTCUSDT_1_1000000_1")
        assert updated is not None
        assert updated.expected_status == OrderState.FILLED

    def test_mark_cancelled(self) -> None:
        """Test marking an order as cancelled."""
        store = ExpectedStateStore()
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,
        )
        store.record_order(order)

        store.mark_cancelled("grinder_BTCUSDT_1_1000000_1")

        updated = store.get_order("grinder_BTCUSDT_1_1000000_1")
        assert updated is not None
        assert updated.expected_status == OrderState.CANCELLED

    def test_mark_filled_nonexistent_order_is_noop(self) -> None:
        """Test mark_filled on non-existent order is a no-op."""
        store = ExpectedStateStore()

        store.mark_filled("nonexistent")

        assert store.order_count == 0

    def test_remove_order(self) -> None:
        """Test removing an order."""
        store = ExpectedStateStore()
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,
        )
        store.record_order(order)

        store.remove_order("grinder_BTCUSDT_1_1000000_1")

        assert store.order_count == 0
        assert store.get_order("grinder_BTCUSDT_1_1000000_1") is None

    def test_get_active_orders_excludes_terminal(self) -> None:
        """Test get_active_orders excludes terminal orders."""
        store = ExpectedStateStore(_clock=lambda: 2000000)

        # Add 3 orders
        for i in range(3):
            order = ExpectedOrder(
                client_order_id=f"grinder_BTCUSDT_1_1000000_{i}",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type="LIMIT",
                price=Decimal("42500.00"),
                orig_qty=Decimal("0.010"),
                ts_created=1000000,
            )
            store.record_order(order)

        # Mark one as filled, one as cancelled
        store.mark_filled("grinder_BTCUSDT_1_1000000_0")
        store.mark_cancelled("grinder_BTCUSDT_1_1000000_1")

        active = store.get_active_orders()

        assert len(active) == 1
        assert active[0].client_order_id == "grinder_BTCUSDT_1_1000000_2"

    def test_get_all_orders_includes_terminal(self) -> None:
        """Test get_all_orders includes terminal orders."""
        store = ExpectedStateStore()

        # Add 2 orders
        for i in range(2):
            order = ExpectedOrder(
                client_order_id=f"grinder_BTCUSDT_1_1000000_{i}",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type="LIMIT",
                price=Decimal("42500.00"),
                orig_qty=Decimal("0.010"),
                ts_created=1000000,
            )
            store.record_order(order)

        # Mark one as filled
        store.mark_filled("grinder_BTCUSDT_1_1000000_0")

        all_orders = store.get_all_orders()

        assert len(all_orders) == 2


class TestExpectedStateStoreRingBuffer:
    """Tests for ring buffer eviction."""

    def test_ring_buffer_eviction_at_capacity(self) -> None:
        """Test ring buffer evicts oldest terminal when at capacity."""
        store = ExpectedStateStore(max_orders=3, _clock=lambda: 10000000)

        # Add 3 orders
        for i in range(3):
            order = ExpectedOrder(
                client_order_id=f"grinder_BTCUSDT_1_1000000_{i}",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type="LIMIT",
                price=Decimal("42500.00"),
                orig_qty=Decimal("0.010"),
                ts_created=1000000 + i,
            )
            store.record_order(order)

        # Mark first as filled (terminal)
        store.mark_filled("grinder_BTCUSDT_1_1000000_0")

        # Add 4th order - should evict the terminal order
        order4 = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_3",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000003,
        )
        store.record_order(order4)

        assert store.order_count == 3
        assert store.get_order("grinder_BTCUSDT_1_1000000_0") is None  # Evicted
        assert store.get_order("grinder_BTCUSDT_1_1000000_1") is not None
        assert store.get_order("grinder_BTCUSDT_1_1000000_2") is not None
        assert store.get_order("grinder_BTCUSDT_1_1000000_3") is not None

    def test_ring_buffer_evicts_oldest_when_no_terminal(self) -> None:
        """Test ring buffer evicts oldest order when no terminal orders."""
        store = ExpectedStateStore(max_orders=3, _clock=lambda: 10000000)

        # Add 3 orders (all active)
        for i in range(3):
            order = ExpectedOrder(
                client_order_id=f"grinder_BTCUSDT_1_1000000_{i}",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type="LIMIT",
                price=Decimal("42500.00"),
                orig_qty=Decimal("0.010"),
                ts_created=1000000 + i,
            )
            store.record_order(order)

        # Add 4th order - should evict oldest
        order4 = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_3",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000003,
        )
        store.record_order(order4)

        assert store.order_count == 3
        assert store.get_order("grinder_BTCUSDT_1_1000000_0") is None  # Evicted
        assert store.get_order("grinder_BTCUSDT_1_1000000_3") is not None


class TestExpectedStateStoreTTL:
    """Tests for TTL eviction."""

    def test_ttl_eviction_of_expired_terminal(self) -> None:
        """Test TTL eviction removes expired terminal orders."""
        current_time = 1000000 + 86_400_001  # 24h + 1ms after creation
        store = ExpectedStateStore(ttl_ms=86_400_000, _clock=lambda: current_time)

        # Add an old terminal order
        old_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_old",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,  # Old
            expected_status=OrderState.FILLED,  # Terminal
        )
        store.record_order(old_order)

        # Add a new order - should trigger eviction
        new_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_new",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=current_time,
        )
        store.record_order(new_order)

        # Old terminal order should be evicted
        assert store.get_order("grinder_BTCUSDT_1_1000000_old") is None
        assert store.get_order("grinder_BTCUSDT_1_1000000_new") is not None

    def test_get_active_orders_excludes_ttl_expired(self) -> None:
        """Test get_active_orders excludes TTL-expired orders."""
        current_time = 1000000 + 86_400_001  # 24h + 1ms after creation
        store = ExpectedStateStore(ttl_ms=86_400_000, _clock=lambda: current_time)

        # Add an old order (but still in store)
        old_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_old",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,  # Expired
        )
        store._orders[old_order.client_order_id] = old_order

        # Add a fresh order
        new_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_new",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=current_time,  # Fresh
        )
        store._orders[new_order.client_order_id] = new_order

        active = store.get_active_orders()

        assert len(active) == 1
        assert active[0].client_order_id == "grinder_BTCUSDT_1_1000000_new"


class TestExpectedStateStorePositions:
    """Tests for position tracking."""

    def test_set_and_get_position(self) -> None:
        """Test setting and getting a position."""
        store = ExpectedStateStore()
        position = ExpectedPosition(
            symbol="BTCUSDT",
            expected_position_amt=Decimal("0"),
            ts_updated=1000000,
        )

        store.set_position(position)

        result = store.get_position("BTCUSDT")
        assert result == position

    def test_get_position_returns_none_for_missing(self) -> None:
        """Test get_position returns None for non-existent symbol."""
        store = ExpectedStateStore()

        result = store.get_position("ETHUSDT")

        assert result is None

    def test_get_all_positions(self) -> None:
        """Test getting all positions."""
        store = ExpectedStateStore()
        pos1 = ExpectedPosition(symbol="BTCUSDT", ts_updated=1000000)
        pos2 = ExpectedPosition(symbol="ETHUSDT", ts_updated=1000000)

        store.set_position(pos1)
        store.set_position(pos2)

        positions = store.get_all_positions()

        assert len(positions) == 2
        symbols = {p.symbol for p in positions}
        assert symbols == {"BTCUSDT", "ETHUSDT"}


class TestExpectedStateStoreClear:
    """Tests for clear functionality."""

    def test_clear_removes_all_state(self) -> None:
        """Test clear removes all orders and positions."""
        store = ExpectedStateStore()

        # Add orders and positions
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1000000,
        )
        store.record_order(order)
        store.set_position(ExpectedPosition(symbol="BTCUSDT"))

        store.clear()

        assert store.order_count == 0
        assert len(store.get_all_positions()) == 0
