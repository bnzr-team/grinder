"""Tests for ObservedStateStore."""

from decimal import Decimal

from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent, FuturesPositionEvent
from grinder.reconcile.observed_state import ObservedStateStore


class TestObservedStateStoreOrders:
    """Tests for order tracking."""

    def test_update_from_order_event(self) -> None:
        """Test updating from FuturesOrderEvent."""
        store = ObservedStateStore()
        event = FuturesOrderEvent(
            ts=1000000,
            symbol="BTCUSDT",
            order_id=12345678,
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("42500.00"),
            qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )

        store.update_from_order_event(event)

        observed = store.get_order("grinder_BTCUSDT_1_1000000_1")
        assert observed is not None
        assert observed.symbol == "BTCUSDT"
        assert observed.status == OrderState.OPEN
        assert observed.source == "stream"

    def test_update_from_order_event_overwrites_existing(self) -> None:
        """Test that new event overwrites existing order."""
        store = ObservedStateStore()

        # First event: NEW
        event1 = FuturesOrderEvent(
            ts=1000000,
            symbol="BTCUSDT",
            order_id=12345678,
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("42500.00"),
            qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
        )
        store.update_from_order_event(event1)

        # Second event: FILLED
        event2 = FuturesOrderEvent(
            ts=2000000,
            symbol="BTCUSDT",
            order_id=12345678,
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            side=OrderSide.BUY,
            status=OrderState.FILLED,
            price=Decimal("42500.00"),
            qty=Decimal("0.010"),
            executed_qty=Decimal("0.010"),
            avg_price=Decimal("42500.00"),
        )
        store.update_from_order_event(event2)

        observed = store.get_order("grinder_BTCUSDT_1_1000000_1")
        assert observed is not None
        assert observed.status == OrderState.FILLED
        assert observed.ts_observed == 2000000

    def test_update_from_rest_orders(self) -> None:
        """Test updating from REST /openOrders response."""
        store = ObservedStateStore()
        orders = [
            {
                "orderId": 12345678,
                "symbol": "BTCUSDT",
                "clientOrderId": "grinder_BTCUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "42500.00",
                "origQty": "0.010",
                "executedQty": "0",
                "avgPrice": "0",
            }
        ]

        store.update_from_rest_orders(orders, ts=2000000)

        observed = store.get_order("grinder_BTCUSDT_1_1000000_1")
        assert observed is not None
        assert observed.source == "rest"
        assert observed.ts_observed == 2000000

    def test_update_from_rest_orders_with_symbol_filter(self) -> None:
        """Test REST update with symbol filter."""
        store = ObservedStateStore()
        orders = [
            {
                "orderId": 1,
                "symbol": "BTCUSDT",
                "clientOrderId": "grinder_BTCUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "42500.00",
                "origQty": "0.010",
                "executedQty": "0",
                "avgPrice": "0",
            },
            {
                "orderId": 2,
                "symbol": "ETHUSDT",
                "clientOrderId": "grinder_ETHUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "3000.00",
                "origQty": "0.1",
                "executedQty": "0",
                "avgPrice": "0",
            },
        ]

        store.update_from_rest_orders(orders, ts=2000000, symbol_filter="BTCUSDT")

        assert store.get_order("grinder_BTCUSDT_1_1000000_1") is not None
        assert store.get_order("grinder_ETHUSDT_1_1000000_1") is None

    def test_update_from_rest_orders_partially_filled(self) -> None:
        """Test REST update maps PARTIALLY_FILLED status."""
        store = ObservedStateStore()
        orders = [
            {
                "orderId": 12345678,
                "symbol": "BTCUSDT",
                "clientOrderId": "grinder_BTCUSDT_1_1000000_1",
                "side": "BUY",
                "status": "PARTIALLY_FILLED",
                "price": "42500.00",
                "origQty": "0.010",
                "executedQty": "0.005",
                "avgPrice": "42500.00",
            }
        ]

        store.update_from_rest_orders(orders, ts=2000000)

        observed = store.get_order("grinder_BTCUSDT_1_1000000_1")
        assert observed is not None
        assert observed.status == OrderState.PARTIALLY_FILLED

    def test_get_open_orders_excludes_terminal(self) -> None:
        """Test get_open_orders excludes terminal orders."""
        store = ObservedStateStore()

        # Add OPEN order
        store.update_from_order_event(
            FuturesOrderEvent(
                ts=1000000,
                symbol="BTCUSDT",
                order_id=1,
                client_order_id="grinder_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        # Add FILLED order
        store.update_from_order_event(
            FuturesOrderEvent(
                ts=2000000,
                symbol="BTCUSDT",
                order_id=2,
                client_order_id="grinder_BTCUSDT_1_1000000_2",
                side=OrderSide.SELL,
                status=OrderState.FILLED,
                price=Decimal("43000.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0.010"),
                avg_price=Decimal("43000.00"),
            )
        )

        open_orders = store.get_open_orders()

        assert len(open_orders) == 1
        assert open_orders[0].client_order_id == "grinder_BTCUSDT_1_1000000_1"

    def test_get_all_orders(self) -> None:
        """Test get_all_orders includes all orders."""
        store = ObservedStateStore()

        # Add 2 orders
        store.update_from_order_event(
            FuturesOrderEvent(
                ts=1000000,
                symbol="BTCUSDT",
                order_id=1,
                client_order_id="grinder_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )
        store.update_from_order_event(
            FuturesOrderEvent(
                ts=2000000,
                symbol="BTCUSDT",
                order_id=2,
                client_order_id="grinder_BTCUSDT_1_1000000_2",
                side=OrderSide.SELL,
                status=OrderState.FILLED,
                price=Decimal("43000.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0.010"),
                avg_price=Decimal("43000.00"),
            )
        )

        all_orders = store.get_all_orders()

        assert len(all_orders) == 2


class TestObservedStateStorePositions:
    """Tests for position tracking."""

    def test_update_from_position_event(self) -> None:
        """Test updating from FuturesPositionEvent."""
        store = ObservedStateStore()
        event = FuturesPositionEvent(
            ts=1000000,
            symbol="BTCUSDT",
            position_amt=Decimal("0.010"),
            entry_price=Decimal("42500.00"),
            unrealized_pnl=Decimal("50.00"),
        )

        store.update_from_position_event(event)

        position = store.get_position("BTCUSDT")
        assert position is not None
        assert position.position_amt == Decimal("0.010")
        assert position.source == "stream"

    def test_update_from_rest_positions(self) -> None:
        """Test updating from REST /positionRisk response."""
        store = ObservedStateStore()
        positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "42500.00",
                "unRealizedProfit": "50.00",
            }
        ]

        store.update_from_rest_positions(positions, ts=2000000)

        position = store.get_position("BTCUSDT")
        assert position is not None
        assert position.position_amt == Decimal("0.010")
        assert position.source == "rest"
        assert position.ts_observed == 2000000

    def test_update_from_rest_positions_with_symbol_filter(self) -> None:
        """Test REST update with symbol filter."""
        store = ObservedStateStore()
        positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "42500.00",
                "unRealizedProfit": "50.00",
            },
            {
                "symbol": "ETHUSDT",
                "positionAmt": "0.5",
                "entryPrice": "3000.00",
                "unRealizedProfit": "10.00",
            },
        ]

        store.update_from_rest_positions(positions, ts=2000000, symbol_filter="BTCUSDT")

        assert store.get_position("BTCUSDT") is not None
        assert store.get_position("ETHUSDT") is None

    def test_get_all_positions(self) -> None:
        """Test getting all positions."""
        store = ObservedStateStore()
        store.update_from_position_event(
            FuturesPositionEvent(
                ts=1000000,
                symbol="BTCUSDT",
                position_amt=Decimal("0.010"),
                entry_price=Decimal("42500.00"),
                unrealized_pnl=Decimal("50.00"),
            )
        )
        store.update_from_position_event(
            FuturesPositionEvent(
                ts=1000000,
                symbol="ETHUSDT",
                position_amt=Decimal("0.5"),
                entry_price=Decimal("3000.00"),
                unrealized_pnl=Decimal("10.00"),
            )
        )

        positions = store.get_all_positions()

        assert len(positions) == 2
        symbols = {p.symbol for p in positions}
        assert symbols == {"BTCUSDT", "ETHUSDT"}


class TestObservedStateStoreSnapshot:
    """Tests for snapshot timestamp tracking."""

    def test_last_snapshot_ts_updated_by_rest_orders(self) -> None:
        """Test last_snapshot_ts is updated by REST orders."""
        store = ObservedStateStore()

        store.update_from_rest_orders([], ts=2000000)

        assert store.last_snapshot_ts == 2000000

    def test_last_snapshot_ts_updated_by_rest_positions(self) -> None:
        """Test last_snapshot_ts is updated by REST positions."""
        store = ObservedStateStore()

        store.update_from_rest_positions([], ts=3000000)

        assert store.last_snapshot_ts == 3000000

    def test_last_snapshot_ts_is_latest(self) -> None:
        """Test last_snapshot_ts reflects most recent snapshot."""
        store = ObservedStateStore()

        store.update_from_rest_orders([], ts=1000000)
        store.update_from_rest_positions([], ts=2000000)
        store.update_from_rest_orders([], ts=3000000)

        assert store.last_snapshot_ts == 3000000


class TestObservedStateStoreClear:
    """Tests for clear functionality."""

    def test_clear_removes_all_state(self) -> None:
        """Test clear removes all orders and positions."""
        store = ObservedStateStore()

        # Add some state
        store.update_from_order_event(
            FuturesOrderEvent(
                ts=1000000,
                symbol="BTCUSDT",
                order_id=1,
                client_order_id="grinder_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )
        store.update_from_position_event(
            FuturesPositionEvent(
                ts=1000000,
                symbol="BTCUSDT",
                position_amt=Decimal("0.010"),
                entry_price=Decimal("42500.00"),
                unrealized_pnl=Decimal("0"),
            )
        )
        store.update_from_rest_orders([], ts=2000000)

        store.clear()

        assert len(store.get_all_orders()) == 0
        assert len(store.get_all_positions()) == 0
        assert store.last_snapshot_ts == 0
