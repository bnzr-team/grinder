"""Tests for reconciliation types."""

from decimal import Decimal

from grinder.core import OrderSide, OrderState
from grinder.reconcile.config import ReconcileConfig
from grinder.reconcile.types import (
    ExpectedOrder,
    ExpectedPosition,
    Mismatch,
    MismatchType,
    ObservedOrder,
    ObservedPosition,
)


class TestExpectedOrder:
    """Tests for ExpectedOrder."""

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Test roundtrip serialization."""
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1704067200000,
            expected_status=OrderState.OPEN,
        )

        d = order.to_dict()
        restored = ExpectedOrder.from_dict(d)

        assert restored == order

    def test_to_json_is_deterministic(self) -> None:
        """Test JSON serialization is deterministic."""
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1704067200000,
        )

        json1 = order.to_json()
        json2 = order.to_json()

        assert json1 == json2

    def test_default_expected_status_is_open(self) -> None:
        """Test default expected_status is OPEN."""
        order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=1704067200000,
        )

        assert order.expected_status == OrderState.OPEN

    def test_from_dict_with_missing_expected_status(self) -> None:
        """Test from_dict defaults expected_status to OPEN."""
        d = {
            "client_order_id": "grinder_BTCUSDT_1_1704067200000_1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "LIMIT",
            "price": "42500.00",
            "orig_qty": "0.010",
            "ts_created": 1704067200000,
        }

        order = ExpectedOrder.from_dict(d)

        assert order.expected_status == OrderState.OPEN


class TestExpectedPosition:
    """Tests for ExpectedPosition."""

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Test roundtrip serialization."""
        position = ExpectedPosition(
            symbol="BTCUSDT",
            expected_position_amt=Decimal("0"),
            ts_updated=1704067200000,
        )

        d = position.to_dict()
        restored = ExpectedPosition.from_dict(d)

        assert restored == position

    def test_default_position_is_zero(self) -> None:
        """Test default expected_position_amt is 0."""
        position = ExpectedPosition(symbol="BTCUSDT")

        assert position.expected_position_amt == Decimal("0")
        assert position.ts_updated == 0


class TestObservedOrder:
    """Tests for ObservedOrder."""

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Test roundtrip serialization."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1704067200000,
            source="stream",
        )

        d = order.to_dict()
        restored = ObservedOrder.from_dict(d)

        assert restored == order

    def test_is_terminal_true_for_filled(self) -> None:
        """Test is_terminal returns True for FILLED."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.FILLED,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0.010"),
            avg_price=Decimal("42500.00"),
            ts_observed=1704067200000,
        )

        assert order.is_terminal() is True

    def test_is_terminal_true_for_cancelled(self) -> None:
        """Test is_terminal returns True for CANCELLED."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.CANCELLED,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1704067200000,
        )

        assert order.is_terminal() is True

    def test_is_terminal_true_for_rejected(self) -> None:
        """Test is_terminal returns True for REJECTED."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.REJECTED,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1704067200000,
        )

        assert order.is_terminal() is True

    def test_is_terminal_true_for_expired(self) -> None:
        """Test is_terminal returns True for EXPIRED."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.EXPIRED,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1704067200000,
        )

        assert order.is_terminal() is True

    def test_is_terminal_false_for_open(self) -> None:
        """Test is_terminal returns False for OPEN."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1704067200000,
        )

        assert order.is_terminal() is False

    def test_is_terminal_false_for_partially_filled(self) -> None:
        """Test is_terminal returns False for PARTIALLY_FILLED."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.PARTIALLY_FILLED,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0.005"),
            avg_price=Decimal("42500.00"),
            ts_observed=1704067200000,
        )

        assert order.is_terminal() is False

    def test_default_source_is_stream(self) -> None:
        """Test default source is 'stream'."""
        order = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=12345678,
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1704067200000,
        )

        assert order.source == "stream"


class TestObservedPosition:
    """Tests for ObservedPosition."""

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Test roundtrip serialization."""
        position = ObservedPosition(
            symbol="BTCUSDT",
            position_amt=Decimal("0.010"),
            entry_price=Decimal("42500.00"),
            unrealized_pnl=Decimal("50.00"),
            ts_observed=1704067200000,
            source="rest",
        )

        d = position.to_dict()
        restored = ObservedPosition.from_dict(d)

        assert restored == position


class TestMismatch:
    """Tests for Mismatch."""

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Test roundtrip serialization."""
        mismatch = Mismatch(
            mismatch_type=MismatchType.ORDER_MISSING_ON_EXCHANGE,
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            expected={"client_order_id": "grinder_BTCUSDT_1_1704067200000_1"},
            observed=None,
            ts_detected=1704067200000,
            action_plan="would cancel order grinder_BTCUSDT_1_1704067200000_1",
        )

        d = mismatch.to_dict()
        restored = Mismatch.from_dict(d)

        assert restored == mismatch

    def test_to_log_extra(self) -> None:
        """Test to_log_extra returns correct fields."""
        mismatch = Mismatch(
            mismatch_type=MismatchType.POSITION_NONZERO_UNEXPECTED,
            symbol="BTCUSDT",
            client_order_id=None,
            expected={"symbol": "BTCUSDT", "expected_position_amt": "0"},
            observed={"symbol": "BTCUSDT", "position_amt": "0.010"},
            ts_detected=1704067200000,
            action_plan="would flatten position BTCUSDT",
        )

        extra = mismatch.to_log_extra()

        assert extra["mismatch_type"] == "POSITION_NONZERO_UNEXPECTED"
        assert extra["symbol"] == "BTCUSDT"
        assert extra["client_order_id"] is None
        assert extra["action_plan"] == "would flatten position BTCUSDT"

    def test_to_json_is_deterministic(self) -> None:
        """Test JSON serialization is deterministic."""
        mismatch = Mismatch(
            mismatch_type=MismatchType.ORDER_EXISTS_UNEXPECTED,
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            expected=None,
            observed={"client_order_id": "grinder_BTCUSDT_1_1704067200000_1"},
            ts_detected=1704067200000,
            action_plan="would cancel unexpected order",
        )

        json1 = mismatch.to_json()
        json2 = mismatch.to_json()

        assert json1 == json2


class TestMismatchType:
    """Tests for MismatchType enum."""

    def test_all_mismatch_types_have_string_values(self) -> None:
        """Test all mismatch types have string values."""
        for mtype in MismatchType:
            assert isinstance(mtype.value, str)
            assert mtype.value == mtype.name

    def test_mismatch_type_values_are_stable(self) -> None:
        """Test mismatch type values are as expected (contract test)."""
        expected_values = {
            "ORDER_MISSING_ON_EXCHANGE",
            "ORDER_EXISTS_UNEXPECTED",
            "ORDER_STATUS_DIVERGENCE",
            "POSITION_NONZERO_UNEXPECTED",
        }

        actual_values = {m.value for m in MismatchType}

        assert actual_values == expected_values


class TestReconcileConfig:
    """Tests for ReconcileConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = ReconcileConfig()

        assert config.order_grace_period_ms == 5000
        assert config.snapshot_interval_sec == 60
        assert config.snapshot_retry_delay_sec == 5
        assert config.snapshot_max_retries == 3
        assert config.expected_max_orders == 200
        assert config.expected_ttl_ms == 86_400_000
        assert config.symbol_filter is None
        assert config.enabled is True

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = ReconcileConfig(
            order_grace_period_ms=10000,
            snapshot_interval_sec=120,
            symbol_filter="BTCUSDT",
            enabled=False,
        )

        assert config.order_grace_period_ms == 10000
        assert config.snapshot_interval_sec == 120
        assert config.symbol_filter == "BTCUSDT"
        assert config.enabled is False
