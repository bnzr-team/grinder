"""Tests for LiveCycleLayerV1 (PR-INV-3).

Tests cover:
- Fill detection (order disappears -> TP generated)
- Pending cancel exclusion (TTL-based)
- TP-on-TP suppression
- Foreign order ignoring
- Deterministic LRU dedup (OrderedDict)
- TTL expiry for pending cancels
- Non-numeric level_id -> TP level_id=0
"""

from collections import OrderedDict
from decimal import Decimal

from grinder.account.contracts import OpenOrderSnap
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.cycle_layer import LiveCycleConfig, LiveCycleLayerV1


def _snap(
    order_id: str,
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: Decimal = Decimal("50000"),
    qty: Decimal = Decimal("0.002"),
) -> OpenOrderSnap:
    """Build a minimal OpenOrderSnap for testing."""
    return OpenOrderSnap(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=price,
        qty=qty,
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=0,
    )


def _make_layer() -> LiveCycleLayerV1:
    """Create a cycle layer with 10bps spacing and 0.10 tick."""
    return LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))


class TestOrderGoneGeneratesTp:
    """Test 1: Order disappearance generates TP PLACE."""

    def test_order_gone_generates_tp(self) -> None:
        layer = _make_layer()
        grid_order = _snap("grinder_d_BTCUSDT_3_1000_1", price=Decimal("50000"))

        # Call 1: establish prev (no actions since prev was empty)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )
        assert actions == []

        # Call 2: order gone -> TP generated
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2000000,
        )
        assert len(actions) == 1
        tp = actions[0]
        assert tp.action_type == ActionType.PLACE
        assert tp.side is not None
        assert tp.side.value == "SELL"  # opposite of BUY
        assert tp.price == Decimal("50050.0")  # 50000 * (1 + 10/10000) = 50050, rounded to 0.10
        assert tp.quantity == Decimal("0.002")
        assert tp.reduce_only is True
        assert tp.reason == "TP_CLOSE"
        assert tp.client_order_id is not None
        assert tp.client_order_id.startswith("grinder_tp_")
        assert tp.level_id == 3


class TestPendingCancelNoTp:
    """Test 2: Pending cancel prevents TP generation."""

    def test_pending_cancel_no_tp(self) -> None:
        layer = _make_layer()
        oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(oid)

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )

        # Register cancel at ts=1000000
        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            order_id=oid,
        )
        layer.register_cancels([cancel_action], ts_ms=1000000)

        # Call 2: order gone but within cancel TTL (1s = 1000ms < 30000ms) -> no TP
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1001000,  # 1s later, within 30s TTL
        )
        assert actions == []


class TestTpOrderGoneNoTpOnTp:
    """Test 3: TP order disappearance does not generate another TP."""

    def test_tp_order_gone_no_tp_on_tp(self) -> None:
        layer = _make_layer()
        grid_order = _snap("grinder_d_BTCUSDT_3_1000_1")
        tp_order = _snap(
            "grinder_tp_BTCUSDT_3_1000_1",
            side="SELL",
            price=Decimal("50005"),
        )

        # Call 1: both grid and TP present
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order, tp_order),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )

        # Call 2: TP gone (filled) -> no action (TP-on-TP suppressed)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=2000000,
        )
        assert actions == []


class TestForeignOrderGoneIgnored:
    """Test 4: Non-grinder orders are ignored."""

    def test_foreign_order_gone_ignored(self) -> None:
        layer = _make_layer()
        grid_order = _snap("grinder_d_BTCUSDT_3_1000_1")
        foreign_order = _snap("manual_123")

        # Call 1: both present (foreign won't parse -> not in prev_orders)
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order, foreign_order),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )

        # Call 2: foreign order gone -> no TP (was never tracked)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=2000000,
        )
        assert actions == []


class TestDedupDeterministicLru:
    """Test 5: Dedup prevents duplicate TP generation."""

    def test_dedup_deterministic_lru(self) -> None:
        layer = _make_layer()
        grid_order = _snap("grinder_d_BTCUSDT_3_1000_1")

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )

        # Call 2: order gone -> 1 TP
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2000000,
        )
        assert len(actions) == 1

        # Call 3: same empty snapshot -> 0 TP (dedup blocks)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=3000000,
        )
        assert actions == []

        # Verify dedup cache is OrderedDict
        assert isinstance(layer._generated_tp_ids, OrderedDict)


class TestPendingCancelTtlExpiry:
    """Test 6: Pending cancel expires after TTL -> treated as fill."""

    def test_pending_cancel_ttl_expiry(self) -> None:
        layer = _make_layer()
        oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(oid)

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )

        # Register cancel at ts=1000000
        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            order_id=oid,
        )
        layer.register_cancels([cancel_action], ts_ms=1000000)

        # Call 2: order gone, ts > TTL (32s = 32000ms > 30000ms) -> cancel expired -> TP generated
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1032000,  # 32000ms later = 32s, > 30s TTL
        )
        assert len(actions) == 1
        assert actions[0].reason == "TP_CLOSE"


class TestNonNumericLevelIdMapsToZero:
    """Test 7: Non-numeric level_id (P1-2 contract)."""

    def test_non_numeric_level_id_maps_to_zero(self) -> None:
        layer = _make_layer()
        # Source order with level_id="cleanup"
        grid_order = _snap("grinder_d_BTCUSDT_cleanup_1000_1")

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1000000,
        )

        # Call 2: order gone -> TP with level_id=0
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2000000,
        )
        assert len(actions) == 1
        tp = actions[0]
        assert tp.level_id == 0
        assert tp.client_order_id is not None
        # Client order ID should contain _0_ for level_id=0
        assert "_0_" in tp.client_order_id
