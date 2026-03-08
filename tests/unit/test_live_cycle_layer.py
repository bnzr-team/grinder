"""Tests for LiveCycleLayerV1 (PR-INV-3 + PR-INV-3b).

Tests cover:
- Fill detection (order disappears -> TP generated)
- Pending cancel exclusion (TTL-based)
- TP-on-TP suppression
- Foreign order ignoring
- Deterministic LRU dedup (OrderedDict)
- TTL expiry for pending cancels
- Non-numeric level_id -> TP level_id=0
- PR-INV-3b: TP expiry (CANCEL after TTL)
- PR-INV-3b: TP expiry disabled (ttl=None/0)
- PR-INV-3b: tp_created_ts cleanup on TP disappearance
- PR-INV-3b: CycleMetrics counters
"""

from collections import OrderedDict
from decimal import Decimal

from grinder.account.contracts import OpenOrderSnap
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.cycle_layer import LiveCycleConfig, LiveCycleLayerV1
from grinder.live.cycle_metrics import get_cycle_metrics, reset_cycle_metrics


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


# --- PR-INV-3b: TP expiry tests ---


class TestTpExpiryEmitsCancel:
    """Test 8: TP older than TTL emits CANCEL."""

    def test_tp_expiry_emits_cancel(self) -> None:
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=60_000)
        )
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid)

        # Call 1: establish prev with grid order
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # Call 2: grid order gone -> TP generated
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )
        assert len(actions) == 1
        tp = actions[0]
        assert tp.action_type == ActionType.PLACE
        tp_oid = tp.client_order_id
        assert tp_oid is not None

        # Call 3: TP now visible in open_orders, NOT expired yet (age=30s < 60s TTL)
        tp_snap = _snap(tp_oid, side="SELL", price=Decimal("50050"))
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,  # 30s after TP created
        )
        # No cancels (TP not yet expired)
        cancel_actions = [a for a in actions if a.action_type == ActionType.CANCEL]
        assert cancel_actions == []

        # Call 4: TP still open, now expired (age=61s > 60s TTL)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,  # 61s after TP created
        )
        cancel_actions = [a for a in actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1
        assert cancel_actions[0].order_id == tp_oid
        assert cancel_actions[0].reason == "TP_EXPIRED"


class TestTpNotExpiredNoCancel:
    """Test 9: TP within TTL does not emit CANCEL."""

    def test_tp_not_expired_no_cancel(self) -> None:
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=300_000)
        )
        tp_oid = "grinder_tp_BTCUSDT_3_2000_1"

        # Manually seed tp_created_ts (simulating TP was generated earlier)
        layer._tp_created_ts[tp_oid] = 1_000_000

        tp_snap = _snap(tp_oid, side="SELL", price=Decimal("50050"))

        # Call: TP in open_orders at ts=1_200_000 (200s < 300s TTL)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=1_200_000,
        )
        cancel_actions = [a for a in actions if a.action_type == ActionType.CANCEL]
        assert cancel_actions == []
        # tp_created_ts still tracked
        assert tp_oid in layer._tp_created_ts


class TestTpRemovedClearsState:
    """Test 10: TP removed from open_orders clears tp_created_ts."""

    def test_tp_removed_from_open_orders_clears_state(self) -> None:
        layer = _make_layer()
        tp_oid = "grinder_tp_BTCUSDT_3_2000_1"

        # Seed tp_created_ts
        layer._tp_created_ts[tp_oid] = 1_000_000

        tp_snap = _snap(tp_oid, side="SELL", price=Decimal("50050"))

        # Call 1: TP present
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=1_100_000,
        )
        assert tp_oid in layer._tp_created_ts

        # Call 2: TP gone (filled by exchange) -> tp_created_ts cleaned up
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1_200_000,
        )
        assert tp_oid not in layer._tp_created_ts


class TestExpiryDisabledNoCancel:
    """Test 11: TTL=None disables expiry."""

    def test_expiry_disabled_no_cancel(self) -> None:
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=None)
        )
        tp_oid = "grinder_tp_BTCUSDT_3_2000_1"
        layer._tp_created_ts[tp_oid] = 1_000_000

        tp_snap = _snap(tp_oid, side="SELL", price=Decimal("50050"))

        # Call: TP in open_orders at ts=9_000_000 (extremely old, but TTL disabled)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=9_000_000,
        )
        cancel_actions = [a for a in actions if a.action_type == ActionType.CANCEL]
        assert cancel_actions == []

    def test_expiry_disabled_zero_ttl(self) -> None:
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=0)
        )
        tp_oid = "grinder_tp_BTCUSDT_3_2000_1"
        layer._tp_created_ts[tp_oid] = 1_000_000

        tp_snap = _snap(tp_oid, side="SELL", price=Decimal("50050"))

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=9_000_000,
        )
        cancel_actions = [a for a in actions if a.action_type == ActionType.CANCEL]
        assert cancel_actions == []


class TestStaleRejectedTpCleanup:
    """Test 12: Rejected TP (never in open_orders) cleaned up by stale TTL."""

    def test_rejected_tp_cleaned_up_by_stale_ttl(self) -> None:
        # TTL=60s -> stale TTL = max(2*60_000, 60_000) = 120_000
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=60_000)
        )
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid)

        # Call 1: establish prev with grid order
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # Call 2: grid order gone -> TP generated (but exchange will reject it)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )
        assert len(actions) == 1
        tp_oid = actions[0].client_order_id
        assert tp_oid is not None

        # Verify: tp_created_ts is tracking the TP
        assert tp_oid in layer._tp_created_ts

        # Call 3: TP never appears in open_orders (rejected by exchange).
        # Time within stale TTL (50s < 120s stale TTL) -> entry survives
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_050_000,
        )
        assert tp_oid in layer._tp_created_ts

        # Call 4: Time exceeds stale TTL (121s > 120s) -> entry cleaned up
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_121_000,
        )
        assert tp_oid not in layer._tp_created_ts

    def test_stale_ttl_with_disabled_tp_ttl(self) -> None:
        # tp_ttl_ms=None -> stale TTL = 600_000 (10 min fallback)
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=None)
        )
        tp_oid = "grinder_tp_BTCUSDT_3_2000_1"
        layer._tp_created_ts[tp_oid] = 1_000_000

        # Call 1: no open orders, time within fallback (500s < 600s)
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1_500_000,
        )
        assert tp_oid in layer._tp_created_ts

        # Call 2: time exceeds fallback (601s > 600s) -> cleaned up
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1_601_000,
        )
        assert tp_oid not in layer._tp_created_ts

    def test_stale_ttl_floor_at_60s(self) -> None:
        # tp_ttl_ms=10_000 (10s) -> stale TTL = max(2*10_000, 60_000) = 60_000
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=10_000)
        )
        tp_oid = "grinder_tp_BTCUSDT_3_2000_1"
        layer._tp_created_ts[tp_oid] = 1_000_000

        # Within floor (50s < 60s) -> survives
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1_050_000,
        )
        assert tp_oid in layer._tp_created_ts

        # Exceeds floor (61s > 60s) -> cleaned up
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=1_061_000,
        )
        assert tp_oid not in layer._tp_created_ts


class TestCycleMetrics:
    """Test 15: CycleMetrics counters (PR-INV-3b)."""

    def test_metrics_recorded_on_fill_and_expiry(self) -> None:
        reset_cycle_metrics()
        layer = LiveCycleLayerV1(
            LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10"), tp_ttl_ms=60_000)
        )
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid)

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # Call 2: fill -> TP generated
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )
        assert len(actions) == 1
        tp_oid = actions[0].client_order_id
        assert tp_oid is not None

        # Verify metrics: 1 tp_generated, 1 fill_candidate(tp_generated)
        m = layer._metrics
        assert m.tp_generated.get("BTCUSDT", 0) == 1
        assert m.fill_candidates.get(("BTCUSDT", "tp_generated"), 0) == 1

        # Call 3: TP visible, then expired
        tp_snap = _snap(tp_oid, side="SELL", price=Decimal("50050"))
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
        )
        cancel_actions = [a for a in actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1
        assert m.tp_expired.get("BTCUSDT", 0) == 1

        # Verify format_metrics produces lines
        lines = m.format_metrics()
        assert any("grinder_cycle_tp_generated_total" in line for line in lines)
        assert any("grinder_cycle_tp_expired_total" in line for line in lines)
        assert any("grinder_cycle_fill_candidates_total" in line for line in lines)

        # Cleanup
        reset_cycle_metrics()


# --- PR-INV-4: Replenish tests ---


def _make_replenish_layer(max_levels: int = 10, enabled: bool = True) -> LiveCycleLayerV1:
    """Create a cycle layer with replenish enabled."""
    return LiveCycleLayerV1(
        LiveCycleConfig(
            spacing_bps=10.0,
            tick_size=Decimal("0.10"),
            replenish_enabled=enabled,
            replenish_max_levels=max_levels,
        )
    )


class TestFillGeneratesTpAndReplenish:
    """Test 16: Fill generates both TP and replenish when enabled."""

    def test_fill_generates_tp_and_replenish(self) -> None:
        reset_cycle_metrics()
        layer = _make_replenish_layer(max_levels=10)
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid, side="BUY", price=Decimal("50000"))

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # Call 2: order gone -> TP + replenish
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]

        # TP: opposite side (SELL), reduce_only=True
        assert len(tp_actions) == 1
        tp = tp_actions[0]
        assert tp.side is not None
        assert tp.side.value == "SELL"
        assert tp.reduce_only is True
        assert tp.client_order_id is not None
        assert tp.client_order_id.startswith("grinder_tp_")

        # Replenish: same side (BUY), reduce_only=False, level_id=4
        assert len(replenish_actions) == 1
        rp = replenish_actions[0]
        assert rp.action_type == ActionType.PLACE
        assert rp.side is not None
        assert rp.side.value == "BUY"
        assert rp.reduce_only is False
        assert rp.level_id == 4  # source was 3, replenish at 3+1=4
        assert rp.reason == "REPLENISH"
        assert rp.client_order_id is not None
        assert rp.client_order_id.startswith("grinder_d_")
        assert not rp.client_order_id.startswith("grinder_tp_")

        # Replenish price: BUY level 4 = mid * (1 - 4*10/10000) = 50000 * 0.996 = 49800.0
        assert rp.price == Decimal("49800.0")

        # Metrics
        m = layer._metrics
        assert m.replenish_generated.get("BTCUSDT", 0) == 1
        reset_cycle_metrics()


class TestReplenishDisabledNoReplenish:
    """Test 17: Replenish disabled -> only TP, no replenish."""

    def test_replenish_disabled_only_tp(self) -> None:
        layer = _make_replenish_layer(max_levels=10, enabled=False)
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid, side="BUY", price=Decimal("50000"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]
        assert len(tp_actions) == 1
        assert len(replenish_actions) == 0


class TestNonNumericLevelIdSkipsReplenish:
    """Test 18: Non-numeric level_id skips replenish (fail-closed)."""

    def test_non_numeric_level_id_no_replenish(self) -> None:
        layer = _make_replenish_layer(max_levels=10)
        grid_oid = "grinder_d_BTCUSDT_cleanup_1000_1"
        grid_order = _snap(grid_oid, side="BUY", price=Decimal("50000"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]
        # TP still generated (level_id=0 for non-numeric)
        assert len(tp_actions) == 1
        assert tp_actions[0].level_id == 0
        # No replenish (non-numeric level_id = fail-closed)
        assert len(replenish_actions) == 0


class TestReplenishRespectsMaxLevels:
    """Test 19: Replenish skipped when level+1 > max_levels."""

    def test_at_max_level_no_replenish(self) -> None:
        layer = _make_replenish_layer(max_levels=3)
        # Level 3 = max_levels, so 3+1=4 > 3 -> no replenish
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid, side="BUY", price=Decimal("50000"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]
        assert len(tp_actions) == 1
        assert len(replenish_actions) == 0

    def test_below_max_level_replenish_ok(self) -> None:
        layer = _make_replenish_layer(max_levels=5)
        # Level 3, 3+1=4 <= 5 -> replenish ok
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid, side="BUY", price=Decimal("50000"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]
        assert len(replenish_actions) == 1
        assert replenish_actions[0].level_id == 4


class TestReplenishSellSide:
    """Test 20: SELL fill generates SELL replenish at correct price."""

    def test_sell_fill_sell_replenish(self) -> None:
        layer = _make_replenish_layer(max_levels=10)
        grid_oid = "grinder_d_BTCUSDT_2_1000_1"
        grid_order = _snap(grid_oid, side="SELL", price=Decimal("51000"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]

        # TP: opposite side (BUY)
        assert len(tp_actions) == 1
        assert tp_actions[0].side is not None
        assert tp_actions[0].side.value == "BUY"

        # Replenish: same side (SELL), level 3
        assert len(replenish_actions) == 1
        rp = replenish_actions[0]
        assert rp.side is not None
        assert rp.side.value == "SELL"
        assert rp.level_id == 3
        # SELL level 3 = mid * (1 + 3*10/10000) = 50000 * 1.003 = 50150.0
        assert rp.price == Decimal("50150.0")


class TestReplenishZeroMaxLevels:
    """Test 21: max_levels=0 disables replenish (fail-closed)."""

    def test_zero_max_levels_no_replenish(self) -> None:
        layer = _make_replenish_layer(max_levels=0, enabled=True)
        grid_oid = "grinder_d_BTCUSDT_3_1000_1"
        grid_order = _snap(grid_oid, side="BUY", price=Decimal("50000"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        replenish_actions = [a for a in actions if a.reason == "REPLENISH"]
        assert len(replenish_actions) == 0


# --- TP Auto-Renew tests (PR-TP-RENEW) ---


def _make_renew_layer(
    *,
    tp_ttl_ms: int = 60_000,
    renew_enabled: bool = True,
    cooldown_ms: int = 60_000,
    max_attempts: int = 3,
) -> LiveCycleLayerV1:
    """Create a cycle layer with TP renew config."""
    return LiveCycleLayerV1(
        LiveCycleConfig(
            spacing_bps=10.0,
            tick_size=Decimal("0.10"),
            tp_ttl_ms=tp_ttl_ms,
            tp_renew_enabled=renew_enabled,
            tp_renew_cooldown_ms=cooldown_ms,
            tp_renew_max_attempts=max_attempts,
        )
    )


def _setup_fill_and_tp(
    layer: LiveCycleLayerV1,
    *,
    fill_side: str = "BUY",
    fill_price: Decimal = Decimal("50000"),
) -> tuple[str, Decimal]:
    """Run layer through fill detection → TP generation, return (tp_client_id, tp_price)."""
    grid_oid = "grinder_d_BTCUSDT_1_1000_1"
    grid_order = _snap(grid_oid, side=fill_side, price=fill_price)

    # Call 1: establish prev
    layer.on_snapshot(
        symbol="BTCUSDT",
        open_orders=(grid_order,),
        mid_price=fill_price,
        ts_ms=1_000_000,
    )
    # Call 2: grid gone → TP generated
    actions = layer.on_snapshot(
        symbol="BTCUSDT",
        open_orders=(),
        mid_price=fill_price,
        ts_ms=2_000_000,
    )
    tp_action = [a for a in actions if a.action_type == ActionType.PLACE and a.reason == "TP_CLOSE"]
    assert len(tp_action) == 1
    tp_id = tp_action[0].client_order_id
    assert tp_id is not None
    tp_price = tp_action[0].price
    assert tp_price is not None
    return tp_id, tp_price


class TestTpAutoRenew:
    """Tests for TP auto-renew on expiry (PR-TP-RENEW)."""

    def test_tp_expiry_with_position_renews(self) -> None:
        """REQ-001: Expired TP with pos_qty != 0 emits [CANCEL old, PLACE new]."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        # TP visible, not expired
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        # TP expired (age=61s > 60s TTL), position open → renew
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )

        cancels = [a for a in actions if a.action_type == ActionType.CANCEL]
        places = [a for a in actions if a.action_type == ActionType.PLACE]
        assert len(cancels) == 1
        assert cancels[0].order_id == tp_id
        assert cancels[0].reason == "TP_RENEW"
        assert len(places) == 1
        assert places[0].reduce_only is True
        assert places[0].reason == "TP_RENEW"
        assert places[0].side is not None
        assert places[0].side.value == "SELL"  # BUY fill → SELL TP

    def test_tp_expiry_without_position_plain_cancel(self) -> None:
        """TP expired with pos_qty=0 falls back to legacy plain cancel."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0"),
        )

        cancels = [a for a in actions if a.action_type == ActionType.CANCEL]
        places = [a for a in actions if a.action_type == ActionType.PLACE]
        assert len(cancels) == 1
        assert cancels[0].reason == "TP_EXPIRED"
        assert len(places) == 0

    def test_renew_cooldown_blocks_spam(self) -> None:
        """REQ-002: Second renewal within cooldown is skipped."""
        layer = _make_renew_layer(cooldown_ms=120_000)
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        # Establish TP in prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        # First expiry → renew succeeds
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )
        assert any(a.reason == "TP_RENEW" and a.action_type == ActionType.PLACE for a in actions)

        # Get new TP id from the place action
        new_tp_id = next(a for a in actions if a.action_type == ActionType.PLACE).client_order_id
        assert new_tp_id is not None
        new_tp_snap = _snap(new_tp_id, side="SELL", price=tp_price)

        # Establish new TP in prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(new_tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_062_000,
            pos_qty=Decimal("0.002"),
        )

        # Second expiry within cooldown (120s) → skipped
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(new_tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_122_000,  # 61s after second TP created, but only 61s after last renew (< 120s cooldown)
            pos_qty=Decimal("0.002"),
        )
        # Should have no renew actions (cooldown blocks)
        renew_places = [
            a for a in actions if a.reason == "TP_RENEW" and a.action_type == ActionType.PLACE
        ]
        assert len(renew_places) == 0

    def test_renew_inflight_latch_prevents_concurrent(self) -> None:
        """REQ-003: Inflight latch prevents concurrent renewals."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        # Manually set inflight latch
        layer._tp_renew_inflight["BTCUSDT"] = True

        # Expiry with inflight → skipped
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )
        renew_actions = [a for a in actions if a.reason == "TP_RENEW"]
        assert len(renew_actions) == 0

    def test_renew_max_attempts_degrades(self) -> None:
        """REQ-004: After max failures, degrades to plain cancel."""
        layer = _make_renew_layer(max_attempts=2)
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        # Exhaust retry budget
        layer._tp_renew_attempts["BTCUSDT"] = 2

        # Expiry with exhausted budget → plain cancel (degraded)
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )
        cancels = [a for a in actions if a.action_type == ActionType.CANCEL]
        places = [a for a in actions if a.action_type == ActionType.PLACE]
        assert len(cancels) == 1
        assert cancels[0].reason == "TP_EXPIRED"
        assert len(places) == 0

    def test_renew_metrics_recorded(self) -> None:
        """REQ-005: Renew outcomes recorded in CycleMetrics."""
        reset_cycle_metrics()
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        # Trigger renew
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )

        metrics = get_cycle_metrics()
        assert metrics.tp_renew.get(("BTCUSDT", "started"), 0) == 1
        assert metrics.tp_renew.get(("BTCUSDT", "renewed"), 0) == 1
        lines = metrics.format_metrics()
        assert any("grinder_cycle_tp_renew_total" in line for line in lines)

    def test_renew_logging(self) -> None:
        """REQ-006: Renew lifecycle produces correct actions (log verified via e2e)."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )

        # Verify renew lifecycle actions: CANCEL(TP_RENEW) + PLACE(TP_RENEW)
        renew_cancel = [
            a for a in actions if a.action_type == ActionType.CANCEL and a.reason == "TP_RENEW"
        ]
        renew_place = [
            a for a in actions if a.action_type == ActionType.PLACE and a.reason == "TP_RENEW"
        ]
        assert len(renew_cancel) == 1
        assert len(renew_place) == 1
        # New TP has different client_order_id
        assert renew_place[0].client_order_id != tp_id

    def test_config_defaults(self) -> None:
        """REQ-007: LiveCycleConfig has correct defaults for renew fields."""
        cfg = LiveCycleConfig()
        assert cfg.tp_renew_enabled is False
        assert cfg.tp_renew_cooldown_ms == 60_000
        assert cfg.tp_renew_max_attempts == 3

    def test_renew_passes_grid_freeze(self) -> None:
        """REQ-008: TP_RENEW actions pass through grid freeze filter."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )

        # Engine filters reason="REPLENISH" when frozen, but TP_RENEW must pass
        renew_actions = [a for a in actions if a.reason == "TP_RENEW"]
        assert len(renew_actions) == 2  # CANCEL + PLACE
        assert all(a.reason != "REPLENISH" for a in renew_actions)

    def test_renew_is_place_then_cancel(self) -> None:
        """REQ-009: Renew emits [PLACE, CANCEL] — PLACE first for atomicity."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer)
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )

        renew_actions = [a for a in actions if a.reason == "TP_RENEW"]
        assert len(renew_actions) == 2
        # PR-P0-TP-RENEW-ATOMIC: PLACE first, CANCEL second
        assert renew_actions[0].action_type == ActionType.PLACE
        assert renew_actions[1].action_type == ActionType.CANCEL
        # No REPLACE action type
        assert all(a.action_type != ActionType.REPLACE for a in actions)

    def test_renew_preserves_original_price(self) -> None:
        """REQ-010: Renewed TP uses original fill_price, not current mid."""
        layer = _make_renew_layer()
        tp_id, tp_price = _setup_fill_and_tp(layer, fill_price=Decimal("50000"))
        tp_snap = _snap(tp_id, side="SELL", price=tp_price)

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("50000"),
            ts_ms=2_030_000,
            pos_qty=Decimal("0.002"),
        )

        # Mid price changed significantly, but renew should use original fill price
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_snap,),
            mid_price=Decimal("55000"),  # mid moved +10%
            ts_ms=2_061_000,
            pos_qty=Decimal("0.002"),
        )

        place_actions = [a for a in actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 1
        # Price should be based on fill_price=50000, not mid=55000
        assert place_actions[0].price == tp_price  # same as original TP


# --- PR-TP-PARTIAL: Partial TP qty mode tests ---


def _make_partial_layer(
    mode: str = "full",
    pct: int = 100,
    per_level_qty: Decimal | None = None,
    step_size: Decimal | None = None,
) -> LiveCycleLayerV1:
    """Create a cycle layer with partial TP qty config."""
    return LiveCycleLayerV1(
        LiveCycleConfig(
            spacing_bps=10.0,
            tick_size=Decimal("0.10"),
            tp_qty_mode=mode,
            tp_qty_pct=pct,
            per_level_qty=per_level_qty,
            step_size=step_size,
        )
    )


class TestComputeTpQty:
    """Tests for _compute_tp_qty (PR-TP-PARTIAL)."""

    def test_full_mode_returns_fill_qty(self) -> None:
        layer = _make_partial_layer(mode="full")
        result = layer._compute_tp_qty(Decimal("0.002"), Decimal("0.004"))
        assert result == Decimal("0.002")

    def test_one_level_mode_returns_per_level_qty(self) -> None:
        layer = _make_partial_layer(
            mode="one_level",
            per_level_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
        )
        result = layer._compute_tp_qty(Decimal("0.002"), Decimal("0.004"))
        assert result == Decimal("0.001")

    def test_one_level_fallback_no_per_level(self) -> None:
        layer = _make_partial_layer(mode="one_level", per_level_qty=None)
        result = layer._compute_tp_qty(Decimal("0.002"), Decimal("0.004"))
        assert result == Decimal("0.002")  # fallback to fill_qty

    def test_pct_mode_50_percent(self) -> None:
        layer = _make_partial_layer(
            mode="pct",
            pct=50,
            step_size=Decimal("0.001"),
        )
        result = layer._compute_tp_qty(Decimal("0.002"), Decimal("0.004"))
        assert result == Decimal("0.002")  # 50% of 0.004 = 0.002

    def test_pct_mode_caps_at_pos_qty(self) -> None:
        layer = _make_partial_layer(
            mode="pct",
            pct=100,
            step_size=Decimal("0.001"),
        )
        # pct=100 of pos_qty=0.004 = 0.004, capped at abs(pos_qty)=0.004
        result = layer._compute_tp_qty(Decimal("0.002"), Decimal("0.004"))
        assert result == Decimal("0.004")

    def test_pct_mode_fallback_no_pos(self) -> None:
        layer = _make_partial_layer(mode="pct", pct=50)
        result = layer._compute_tp_qty(Decimal("0.002"), None)
        assert result == Decimal("0.002")  # fallback to fill_qty

    def test_step_size_rounding(self) -> None:
        layer = _make_partial_layer(
            mode="pct",
            pct=50,
            step_size=Decimal("0.001"),
        )
        # 50% of 0.005 = 0.0025, rounded down to 0.002
        result = layer._compute_tp_qty(Decimal("0.005"), Decimal("0.005"))
        assert result == Decimal("0.002")

    def test_too_small_returns_zero(self) -> None:
        layer = _make_partial_layer(
            mode="pct",
            pct=1,  # 1% of 0.002 = 0.00002, below step_size=0.001
            step_size=Decimal("0.001"),
        )
        result = layer._compute_tp_qty(Decimal("0.002"), Decimal("0.002"))
        assert result == Decimal("0")


class TestPartialTpGeneration:
    """Tests for partial TP qty in on_snapshot (PR-TP-PARTIAL)."""

    def test_one_level_tp_qty_in_snapshot(self) -> None:
        layer = _make_partial_layer(
            mode="one_level",
            per_level_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
        )
        grid_order = _snap(
            "grinder_d_BTCUSDT_3_1000_1",
            side="BUY",
            price=Decimal("50000"),
            qty=Decimal("0.002"),
        )

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
            pos_qty=Decimal("0.004"),
        )

        # Call 2: order gone → TP with one_level qty
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
            pos_qty=Decimal("0.004"),
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        assert len(tp_actions) == 1
        assert tp_actions[0].quantity == Decimal("0.001")

    def test_pct_tp_qty_in_snapshot(self) -> None:
        layer = _make_partial_layer(
            mode="pct",
            pct=50,
            step_size=Decimal("0.001"),
        )
        grid_order = _snap(
            "grinder_d_BTCUSDT_3_1000_1",
            side="BUY",
            price=Decimal("50000"),
            qty=Decimal("0.002"),
        )

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
            pos_qty=Decimal("0.004"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
            pos_qty=Decimal("0.004"),
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        assert len(tp_actions) == 1
        assert tp_actions[0].quantity == Decimal("0.002")  # 50% of 0.004

    def test_full_mode_tp_qty_unchanged(self) -> None:
        layer = _make_partial_layer(mode="full")
        grid_order = _snap(
            "grinder_d_BTCUSDT_3_1000_1",
            side="BUY",
            price=Decimal("50000"),
            qty=Decimal("0.002"),
        )

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
            pos_qty=Decimal("0.004"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
            pos_qty=Decimal("0.004"),
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        assert len(tp_actions) == 1
        assert tp_actions[0].quantity == Decimal("0.002")  # snap.qty unchanged


class TestPartialTpSkipTooSmall:
    """Tests for TP skip when partial qty too small (PR-TP-PARTIAL)."""

    def test_too_small_skips_tp(self) -> None:
        reset_cycle_metrics()
        layer = _make_partial_layer(
            mode="pct",
            pct=1,  # 1% of 0.002 = 0.00002 < step_size 0.001
            step_size=Decimal("0.001"),
        )
        grid_order = _snap(
            "grinder_d_BTCUSDT_3_1000_1",
            side="BUY",
            price=Decimal("50000"),
            qty=Decimal("0.002"),
        )

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
            pos_qty=Decimal("0.002"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
            pos_qty=Decimal("0.002"),
        )

        # No TP generated — qty too small
        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        assert len(tp_actions) == 0

        # Metric recorded
        m = get_cycle_metrics()
        assert m.fill_candidates.get(("BTCUSDT", "tp_qty_too_small"), 0) == 1
        reset_cycle_metrics()

    def test_one_level_caps_at_pos_qty(self) -> None:
        layer = _make_partial_layer(
            mode="one_level",
            per_level_qty=Decimal("0.010"),  # bigger than pos_qty
            step_size=Decimal("0.001"),
        )
        grid_order = _snap(
            "grinder_d_BTCUSDT_3_1000_1",
            side="BUY",
            price=Decimal("50000"),
            qty=Decimal("0.002"),
        )

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(grid_order,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
            pos_qty=Decimal("0.003"),
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
            pos_qty=Decimal("0.003"),
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        assert len(tp_actions) == 1
        assert tp_actions[0].quantity == Decimal("0.003")  # capped at pos_qty


# --- PR-ROLL-2: TP slot takeover tests ---


class TestTPSlotTakeover:
    """Tests for TP_SLOT_TAKEOVER (PR-ROLL-2).

    When a TP is created, cancel the farthest same-side grid order
    to keep total opposite-side order count constant.
    """

    def test_long_tp_sell_cancels_farthest_sell(self) -> None:
        """LONG fill: TP SELL + CANCEL farthest (max-price) SELL grid."""
        layer = _make_layer()
        buy_grid = _snap("grinder_d_BTCUSDT_1_1000_1", side="BUY", price=Decimal("49900"))
        sell1 = _snap("grinder_d_BTCUSDT_1_1000_2", side="SELL", price=Decimal("50100"))
        sell2 = _snap("grinder_d_BTCUSDT_2_1000_2", side="SELL", price=Decimal("50200"))
        sell3 = _snap("grinder_d_BTCUSDT_3_1000_2", side="SELL", price=Decimal("50300"))

        # Call 1: establish prev
        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(buy_grid, sell1, sell2, sell3),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # Call 2: BUY gone (fill) -> TP SELL + CANCEL farthest SELL
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(sell1, sell2, sell3),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        takeover_actions = [a for a in actions if a.reason == "TP_SLOT_TAKEOVER"]
        assert len(tp_actions) == 1
        assert tp_actions[0].side is not None
        assert tp_actions[0].side.value == "SELL"
        assert len(takeover_actions) == 1
        assert takeover_actions[0].action_type == ActionType.CANCEL
        assert takeover_actions[0].order_id == "grinder_d_BTCUSDT_3_1000_2"  # max price

    def test_short_tp_buy_cancels_farthest_buy(self) -> None:
        """SHORT fill: TP BUY + CANCEL farthest (min-price) BUY grid."""
        layer = _make_layer()
        sell_grid = _snap("grinder_d_BTCUSDT_1_1000_1", side="SELL", price=Decimal("50100"))
        buy1 = _snap("grinder_d_BTCUSDT_1_1000_2", side="BUY", price=Decimal("49900"))
        buy2 = _snap("grinder_d_BTCUSDT_2_1000_2", side="BUY", price=Decimal("49800"))
        buy3 = _snap("grinder_d_BTCUSDT_3_1000_2", side="BUY", price=Decimal("49700"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(sell_grid, buy1, buy2, buy3),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(buy1, buy2, buy3),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        takeover_actions = [a for a in actions if a.reason == "TP_SLOT_TAKEOVER"]
        assert len(tp_actions) == 1
        assert tp_actions[0].side is not None
        assert tp_actions[0].side.value == "BUY"
        assert len(takeover_actions) == 1
        assert takeover_actions[0].action_type == ActionType.CANCEL
        assert takeover_actions[0].order_id == "grinder_d_BTCUSDT_3_1000_2"  # min price

    def test_no_same_side_grid_skip(self) -> None:
        """No same-side grid orders: TP placed, no CANCEL."""
        layer = _make_layer()
        buy_grid = _snap("grinder_d_BTCUSDT_1_1000_1", side="BUY", price=Decimal("49900"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(buy_grid,),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # BUY gone, no SELL grid orders available
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        takeover_actions = [a for a in actions if a.reason == "TP_SLOT_TAKEOVER"]
        assert len(tp_actions) == 1
        assert len(takeover_actions) == 0

    def test_tp_not_cancelled(self) -> None:
        """TP orders on same side are NOT eligible for slot takeover."""
        layer = _make_layer()
        buy_grid = _snap("grinder_d_BTCUSDT_1_1000_1", side="BUY", price=Decimal("49900"))
        tp_sell = _snap("grinder_tp_BTCUSDT_1_1000_1", side="SELL", price=Decimal("50050"))
        grid_sell = _snap("grinder_d_BTCUSDT_2_1000_2", side="SELL", price=Decimal("50100"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(buy_grid, tp_sell, grid_sell),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(tp_sell, grid_sell),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        takeover_actions = [a for a in actions if a.reason == "TP_SLOT_TAKEOVER"]
        assert len(takeover_actions) == 1
        # Must cancel grid_sell, NOT tp_sell
        assert takeover_actions[0].order_id == "grinder_d_BTCUSDT_2_1000_2"

    def test_multi_fill_claims_different_orders(self) -> None:
        """2 BUY fills in same tick: 2 TP SELLs + 2 different SELL grid CANCELs."""
        layer = _make_layer()
        buy1 = _snap("grinder_d_BTCUSDT_1_1000_1", side="BUY", price=Decimal("49900"))
        buy2 = _snap("grinder_d_BTCUSDT_2_1000_1", side="BUY", price=Decimal("49800"))
        sell1 = _snap("grinder_d_BTCUSDT_1_1000_2", side="SELL", price=Decimal("50100"))
        sell2 = _snap("grinder_d_BTCUSDT_2_1000_2", side="SELL", price=Decimal("50200"))
        sell3 = _snap("grinder_d_BTCUSDT_3_1000_2", side="SELL", price=Decimal("50300"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(buy1, buy2, sell1, sell2, sell3),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        # Both BUYs gone: 2 fills in same tick
        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(sell1, sell2, sell3),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        tp_actions = [a for a in actions if a.reason == "TP_CLOSE"]
        takeover_actions = [a for a in actions if a.reason == "TP_SLOT_TAKEOVER"]
        assert len(tp_actions) == 2
        assert len(takeover_actions) == 2
        # Two different SELL grid orders cancelled (not the same one twice)
        cancelled_ids = {a.order_id for a in takeover_actions}
        assert len(cancelled_ids) == 2
        assert "grinder_d_BTCUSDT_3_1000_2" in cancelled_ids  # farthest first
        assert "grinder_d_BTCUSDT_2_1000_2" in cancelled_ids  # next farthest

    def test_cancelled_order_registered_as_pending(self) -> None:
        """Cancelled grid order is in _pending_cancels (prevents false fill)."""
        layer = _make_layer()
        buy_grid = _snap("grinder_d_BTCUSDT_1_1000_1", side="BUY", price=Decimal("49900"))
        sell_grid = _snap("grinder_d_BTCUSDT_1_1000_2", side="SELL", price=Decimal("50100"))

        layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(buy_grid, sell_grid),
            mid_price=Decimal("50000"),
            ts_ms=1_000_000,
        )

        actions = layer.on_snapshot(
            symbol="BTCUSDT",
            open_orders=(sell_grid,),
            mid_price=Decimal("50000"),
            ts_ms=2_000_000,
        )

        takeover_actions = [a for a in actions if a.reason == "TP_SLOT_TAKEOVER"]
        assert len(takeover_actions) == 1
        cancelled_id = takeover_actions[0].order_id
        # Verify _pending_cancels registration
        assert cancelled_id in layer._pending_cancels
        assert layer._pending_cancels[cancelled_id] == 2_000_000
