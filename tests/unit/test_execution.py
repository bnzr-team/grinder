"""Tests for execution engine v0.

Tests verify the requirements from docs/ROADMAP.md DoD for PR-017:
- HARD reset: cancel all + place new ladder
- PAUSE mode: cancel all orders
- Reconcile: add missing, remove extra, preserve matching
- Determinism: same plan+state → same actions
- Order ID generation: deterministic format
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from grinder.core import GridMode, MarketRegime, OrderSide, OrderState, ResetAction
from grinder.execution import (
    ActionType,
    ExecutionEngine,
    ExecutionEvent,
    ExecutionState,
    NoOpExchangePort,
    OrderRecord,
)
from grinder.execution.constraint_provider import ConstraintProvider
from grinder.execution.engine import ExecutionEngineConfig, SymbolConstraints, floor_to_step
from grinder.execution.metrics import ExecutionMetrics
from grinder.features.l2_types import L2FeatureSnapshot
from grinder.policies.base import GridPlan

# --- Fixtures ---


@pytest.fixture
def port() -> NoOpExchangePort:
    """Fresh NoOpExchangePort for each test."""
    return NoOpExchangePort()


@pytest.fixture
def engine(port: NoOpExchangePort) -> ExecutionEngine:
    """Execution engine with default precision."""
    return ExecutionEngine(port=port, price_precision=2, quantity_precision=3)


@pytest.fixture
def bilateral_plan() -> GridPlan:
    """Standard bilateral grid plan for testing."""
    return GridPlan(
        mode=GridMode.BILATERAL,
        center_price=Decimal("50000"),
        spacing_bps=10.0,
        levels_up=3,
        levels_down=3,
        size_schedule=[Decimal("0.1"), Decimal("0.2"), Decimal("0.3")],
        skew_bps=0.0,
        regime=MarketRegime.RANGE,
        reset_action=ResetAction.NONE,
        reason_codes=["TEST"],
    )


@pytest.fixture
def pause_plan() -> GridPlan:
    """PAUSE mode plan that cancels all orders."""
    return GridPlan(
        mode=GridMode.PAUSE,
        center_price=Decimal("50000"),
        spacing_bps=10.0,
        levels_up=3,
        levels_down=3,
        size_schedule=[Decimal("0.1")],
        regime=MarketRegime.RANGE,
        reset_action=ResetAction.NONE,
        reason_codes=["PAUSE"],
    )


@pytest.fixture
def hard_reset_plan() -> GridPlan:
    """Plan with HARD reset action."""
    return GridPlan(
        mode=GridMode.BILATERAL,
        center_price=Decimal("50000"),
        spacing_bps=10.0,
        levels_up=3,
        levels_down=3,
        size_schedule=[Decimal("0.1")],
        regime=MarketRegime.RANGE,
        reset_action=ResetAction.HARD,
        reason_codes=["HARD_RESET"],
    )


@pytest.fixture
def soft_reset_plan() -> GridPlan:
    """Plan with SOFT reset action."""
    return GridPlan(
        mode=GridMode.BILATERAL,
        center_price=Decimal("50000"),
        spacing_bps=10.0,
        levels_up=3,
        levels_down=3,
        size_schedule=[Decimal("0.1")],
        regime=MarketRegime.RANGE,
        reset_action=ResetAction.SOFT,
        reason_codes=["SOFT_RESET"],
    )


@pytest.fixture
def empty_state() -> ExecutionState:
    """Empty execution state."""
    return ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)


# --- Tests: PAUSE/EMERGENCY Mode ---


class TestPauseMode:
    """Tests for PAUSE mode cancelling all orders."""

    def test_pause_cancels_all_orders(
        self,
        engine: ExecutionEngine,
        pause_plan: GridPlan,
    ) -> None:
        """Test PAUSE mode generates cancel actions for all open orders."""
        # Setup: state with existing orders
        state = ExecutionState(
            open_orders={
                "order1": OrderRecord(
                    order_id="order1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("49000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
                "order2": OrderRecord(
                    order_id="order2",
                    symbol="BTCUSDT",
                    side=OrderSide.SELL,
                    price=Decimal("51000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(pause_plan, "BTCUSDT", state, ts=2000)

        # All orders should be cancelled
        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 2
        assert {a.order_id for a in cancel_actions} == {"order1", "order2"}

        # No place actions
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 0

    def test_pause_with_no_orders(
        self,
        engine: ExecutionEngine,
        pause_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test PAUSE mode with no existing orders."""
        result = engine.evaluate(pause_plan, "BTCUSDT", empty_state, ts=1000)

        assert len(result.actions) == 0

    def test_pause_emits_cancel_event(
        self,
        engine: ExecutionEngine,
        pause_plan: GridPlan,
    ) -> None:
        """Test PAUSE mode emits CANCEL_ALL event."""
        state = ExecutionState(
            open_orders={
                "order1": OrderRecord(
                    order_id="order1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("49000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(pause_plan, "BTCUSDT", state, ts=2000)

        assert len(result.events) == 1
        event = result.events[0]
        assert event.event_type == "CANCEL_ALL_PAUSE"
        assert event.details["cancelled_count"] == 1


class TestEmergencyMode:
    """Tests for EMERGENCY mode (same behavior as PAUSE)."""

    def test_emergency_cancels_all(self, engine: ExecutionEngine) -> None:
        """Test EMERGENCY mode cancels all orders."""
        emergency_plan = GridPlan(
            mode=GridMode.EMERGENCY,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.1")],
            reason_codes=["EMERGENCY"],
        )
        state = ExecutionState(
            open_orders={
                "order1": OrderRecord(
                    order_id="order1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("49000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(emergency_plan, "BTCUSDT", state, ts=2000)

        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1
        assert result.events[0].event_type == "CANCEL_ALL_EMERGENCY"


# --- Tests: HARD Reset ---


class TestHardReset:
    """Tests for HARD reset: cancel all + place new ladder."""

    def test_hard_reset_cancels_existing(
        self,
        engine: ExecutionEngine,
        hard_reset_plan: GridPlan,
    ) -> None:
        """Test HARD reset cancels all existing orders."""
        state = ExecutionState(
            open_orders={
                "old1": OrderRecord(
                    order_id="old1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("48000"),
                    quantity=Decimal("0.5"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
                "old2": OrderRecord(
                    order_id="old2",
                    symbol="BTCUSDT",
                    side=OrderSide.SELL,
                    price=Decimal("52000"),
                    quantity=Decimal("0.5"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(hard_reset_plan, "BTCUSDT", state, ts=2000)

        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 2
        assert all(a.reason == "HARD_RESET" for a in cancel_actions)

    def test_hard_reset_places_new_grid(
        self,
        engine: ExecutionEngine,
        hard_reset_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test HARD reset places complete new grid."""
        result = engine.evaluate(hard_reset_plan, "BTCUSDT", empty_state, ts=1000)

        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        # 3 levels up (SELL) + 3 levels down (BUY) = 6 orders
        assert len(place_actions) == 6

        sell_actions = [a for a in place_actions if a.side == OrderSide.SELL]
        buy_actions = [a for a in place_actions if a.side == OrderSide.BUY]
        assert len(sell_actions) == 3
        assert len(buy_actions) == 3

    def test_hard_reset_event(
        self,
        engine: ExecutionEngine,
        hard_reset_plan: GridPlan,
    ) -> None:
        """Test HARD reset emits correct event."""
        state = ExecutionState(
            open_orders={
                "old1": OrderRecord(
                    order_id="old1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("48000"),
                    quantity=Decimal("0.5"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(hard_reset_plan, "BTCUSDT", state, ts=2000)

        assert len(result.events) == 1
        event = result.events[0]
        assert event.event_type == "HARD_RESET"
        assert event.details["cancelled_count"] == 1
        assert event.details["placed_count"] == 6


# --- Tests: SOFT Reset ---


class TestSoftReset:
    """Tests for SOFT reset: cancel/replace non-conforming orders."""

    def test_soft_reset_replaces_mismatched_order(
        self,
        engine: ExecutionEngine,
        soft_reset_plan: GridPlan,
    ) -> None:
        """Test SOFT reset replaces order with wrong price."""
        # Create state with order that has wrong price
        state = ExecutionState(
            open_orders={
                "order1": OrderRecord(
                    order_id="order1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("48000"),  # Wrong price
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(soft_reset_plan, "BTCUSDT", state, ts=2000)

        # Should have cancel for mismatched order
        cancel_actions = [
            a
            for a in result.actions
            if a.action_type == ActionType.CANCEL and a.reason == "SOFT_RESET_REPLACE"
        ]
        assert len(cancel_actions) == 1
        assert cancel_actions[0].order_id == "order1"

        # Should have place for replacement
        place_replace = [
            a
            for a in result.actions
            if a.action_type == ActionType.PLACE and a.reason == "SOFT_RESET_REPLACE"
        ]
        assert len(place_replace) == 1


# --- Tests: Reconcile (NONE reset) ---


class TestReconcileLogic:
    """Tests for reconcile: add missing, remove extra, preserve matching."""

    def test_reconcile_places_missing_levels(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test reconcile places all missing levels from scratch."""
        result = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)

        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        # 3 SELL + 3 BUY = 6 orders
        assert len(place_actions) == 6
        assert all(a.reason == "RECONCILE_ADD" for a in place_actions)

    def test_reconcile_removes_extra_orders(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
    ) -> None:
        """Test reconcile cancels orders not in current grid."""
        # State with order at level 10 (not in 3-level grid)
        state = ExecutionState(
            open_orders={
                "extra1": OrderRecord(
                    order_id="extra1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("45000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=10,  # Outside grid
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(bilateral_plan, "BTCUSDT", state, ts=2000)

        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1
        assert cancel_actions[0].reason == "RECONCILE_REMOVE"

    def test_reconcile_preserves_matching_orders(
        self,
        engine: ExecutionEngine,
    ) -> None:
        """Test reconcile preserves orders that match grid levels exactly."""
        # Create plan and compute expected price for level 1 BUY
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.1")],
            reset_action=ResetAction.NONE,
            reason_codes=["TEST"],
        )

        # Level 1 BUY price = 50000 * (1 - 10/10000) = 50000 * 0.999 = 49950
        expected_buy_price = Decimal("49950.00")

        state = ExecutionState(
            open_orders={
                "matching": OrderRecord(
                    order_id="matching",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=expected_buy_price,
                    quantity=Decimal("0.100"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(plan, "BTCUSDT", state, ts=2000)

        # Should not cancel matching order
        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 0

        # Should place missing SELL order only
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 1
        assert place_actions[0].side == OrderSide.SELL

    def test_reconcile_idempotent(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test reconcile is idempotent: second call on result state produces no actions."""
        # First evaluation
        result1 = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)
        assert len(result1.actions) > 0

        # Second evaluation with resulting state
        result2 = engine.evaluate(bilateral_plan, "BTCUSDT", result1.state, ts=2000)

        # Should have no actions (grid is already correct)
        actions_with_impact = [
            a for a in result2.actions if a.action_type in (ActionType.PLACE, ActionType.CANCEL)
        ]
        assert len(actions_with_impact) == 0


# --- Tests: Determinism ---


class TestDeterminism:
    """Tests for deterministic behavior."""

    def test_same_plan_same_actions(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test same plan+state produces identical actions."""
        result1 = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)
        result2 = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)

        # Same number of actions
        assert len(result1.actions) == len(result2.actions)

        # Same plan digest
        assert result1.plan_digest == result2.plan_digest

        # Actions should have same structure (prices, quantities, sides)
        for a1, a2 in zip(result1.actions, result2.actions, strict=True):
            assert a1.action_type == a2.action_type
            assert a1.side == a2.side
            assert a1.price == a2.price
            assert a1.quantity == a2.quantity
            assert a1.level_id == a2.level_id

    def test_plan_digest_changes_with_plan(
        self,
        engine: ExecutionEngine,
        empty_state: ExecutionState,
    ) -> None:
        """Test plan digest changes when plan changes."""
        plan1 = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.1")],
            reason_codes=["TEST"],
        )
        plan2 = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("51000"),  # Different center
            spacing_bps=10.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.1")],
            reason_codes=["TEST"],
        )

        result1 = engine.evaluate(plan1, "BTCUSDT", empty_state, ts=1000)
        result2 = engine.evaluate(plan2, "BTCUSDT", empty_state, ts=1000)

        assert result1.plan_digest != result2.plan_digest


# --- Tests: NoOpExchangePort ---


class TestNoOpExchangePort:
    """Tests for NoOpExchangePort stub."""

    def test_place_order_returns_deterministic_id(self, port: NoOpExchangePort) -> None:
        """Test place_order generates deterministic order ID."""
        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            level_id=1,
            ts=1000,
        )

        # ID format: {symbol}:{ts}:{level_id}:{side}:{counter}
        assert order_id == "BTCUSDT:1000:1:BUY:1"

    def test_order_ids_increment(self, port: NoOpExchangePort) -> None:
        """Test order IDs increment counter."""
        id1 = port.place_order("BTCUSDT", OrderSide.BUY, Decimal("50000"), Decimal("0.1"), 1, 1000)
        id2 = port.place_order("BTCUSDT", OrderSide.SELL, Decimal("51000"), Decimal("0.1"), 1, 1000)

        assert id1 == "BTCUSDT:1000:1:BUY:1"
        assert id2 == "BTCUSDT:1000:1:SELL:2"

    def test_cancel_order_returns_true_for_existing(self, port: NoOpExchangePort) -> None:
        """Test cancel_order returns True for existing open order."""
        # First place an order
        order_id = port.place_order(
            "BTCUSDT", OrderSide.BUY, Decimal("50000"), Decimal("0.1"), 1, 1000
        )

        # Then cancel it
        result = port.cancel_order(order_id)
        assert result is True

    def test_cancel_order_returns_false_for_nonexistent(self, port: NoOpExchangePort) -> None:
        """Test cancel_order returns False for non-existent order."""
        result = port.cancel_order("nonexistent_order")
        assert result is False

    def test_replace_order_returns_new_id(self, port: NoOpExchangePort) -> None:
        """Test replace_order returns new order ID."""
        # First place an order
        old_id = port.place_order(
            "BTCUSDT", OrderSide.BUY, Decimal("50000"), Decimal("0.1"), 1, 1000
        )

        # Then replace it
        new_id = port.replace_order(
            order_id=old_id,
            new_price=Decimal("51000"),
            new_quantity=Decimal("0.2"),
            ts=2000,
        )

        # Replace places new order with same level_id
        assert new_id == "BTCUSDT:2000:1:BUY:2"
        assert new_id != old_id

    def test_fetch_open_orders_empty(self, port: NoOpExchangePort) -> None:
        """Test fetch_open_orders returns empty list (stub)."""
        orders = port.fetch_open_orders("BTCUSDT")
        assert orders == []


# --- Tests: Grid Level Computation ---


class TestGridLevelComputation:
    """Tests for grid level price computation."""

    def test_sell_levels_above_center(
        self,
        engine: ExecutionEngine,
        empty_state: ExecutionState,
    ) -> None:
        """Test SELL levels are placed above center price."""
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=2,
            levels_down=0,
            size_schedule=[Decimal("0.1")],
            reason_codes=["TEST"],
        )

        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2

        for action in place_actions:
            assert action.side == OrderSide.SELL
            assert action.price is not None
            assert action.price > Decimal("50000")

    def test_buy_levels_below_center(
        self,
        engine: ExecutionEngine,
        empty_state: ExecutionState,
    ) -> None:
        """Test BUY levels are placed below center price."""
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=0,
            levels_down=2,
            size_schedule=[Decimal("0.1")],
            reason_codes=["TEST"],
        )

        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2

        for action in place_actions:
            assert action.side == OrderSide.BUY
            assert action.price is not None
            assert action.price < Decimal("50000")

    def test_uni_long_no_sell_orders(
        self,
        engine: ExecutionEngine,
        empty_state: ExecutionState,
    ) -> None:
        """Test UNI_LONG mode produces no SELL orders."""
        plan = GridPlan(
            mode=GridMode.UNI_LONG,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.1")],
            reason_codes=["UNI_LONG"],
        )

        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        sell_actions = [a for a in place_actions if a.side == OrderSide.SELL]
        buy_actions = [a for a in place_actions if a.side == OrderSide.BUY]

        assert len(sell_actions) == 0
        assert len(buy_actions) == 3

    def test_uni_short_no_buy_orders(
        self,
        engine: ExecutionEngine,
        empty_state: ExecutionState,
    ) -> None:
        """Test UNI_SHORT mode produces no BUY orders."""
        plan = GridPlan(
            mode=GridMode.UNI_SHORT,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.1")],
            reason_codes=["UNI_SHORT"],
        )

        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        sell_actions = [a for a in place_actions if a.side == OrderSide.SELL]
        buy_actions = [a for a in place_actions if a.side == OrderSide.BUY]

        assert len(sell_actions) == 3
        assert len(buy_actions) == 0


# --- Tests: State Updates ---


class TestStateUpdates:
    """Tests for execution state updates."""

    def test_tick_counter_increments(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test tick counter increments on each evaluation."""
        assert empty_state.tick_counter == 0

        result1 = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)
        assert result1.state.tick_counter == 1

        result2 = engine.evaluate(bilateral_plan, "BTCUSDT", result1.state, ts=2000)
        assert result2.state.tick_counter == 2

    def test_placed_orders_added_to_state(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test placed orders are added to state."""
        result = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)

        # Should have 6 orders in state (3 SELL + 3 BUY)
        assert len(result.state.open_orders) == 6

        # All should be OPEN state
        for order in result.state.open_orders.values():
            assert order.state == OrderState.OPEN

    def test_cancelled_orders_marked_in_state(
        self,
        engine: ExecutionEngine,
        pause_plan: GridPlan,
    ) -> None:
        """Test cancelled orders are marked as CANCELLED in state."""
        state = ExecutionState(
            open_orders={
                "order1": OrderRecord(
                    order_id="order1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("49000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(pause_plan, "BTCUSDT", state, ts=2000)

        assert result.state.open_orders["order1"].state == OrderState.CANCELLED

    def test_last_plan_digest_updated(
        self,
        engine: ExecutionEngine,
        bilateral_plan: GridPlan,
        empty_state: ExecutionState,
    ) -> None:
        """Test last_plan_digest is updated in state."""
        assert empty_state.last_plan_digest == ""

        result = engine.evaluate(bilateral_plan, "BTCUSDT", empty_state, ts=1000)

        assert result.state.last_plan_digest != ""
        assert result.state.last_plan_digest == result.plan_digest


# --- Tests: Metrics ---


class TestExecutionMetrics:
    """Tests for execution metrics."""

    def test_record_intent(self) -> None:
        """Test recording intents."""
        metrics = ExecutionMetrics()

        metrics.record_intent(ActionType.PLACE)
        metrics.record_intent(ActionType.PLACE)
        metrics.record_intent(ActionType.CANCEL)

        assert metrics.intents_total["PLACE"] == 2
        assert metrics.intents_total["CANCEL"] == 1

    def test_record_event(self) -> None:
        """Test recording events."""
        metrics = ExecutionMetrics()

        event = ExecutionEvent(ts=1000, event_type="RECONCILE", symbol="BTCUSDT")
        metrics.record_event(event)
        metrics.record_event(event)

        assert metrics.exec_events_total["RECONCILE"] == 2

    def test_set_orders_open(self) -> None:
        """Test setting open orders gauge."""
        metrics = ExecutionMetrics()

        metrics.set_orders_open("BTCUSDT", OrderSide.BUY, 3)
        metrics.set_orders_open("BTCUSDT", OrderSide.SELL, 5)

        assert metrics.orders_open[("BTCUSDT", "BUY")] == 3
        assert metrics.orders_open[("BTCUSDT", "SELL")] == 5

    def test_get_metrics(self) -> None:
        """Test getting all metrics as dict."""
        metrics = ExecutionMetrics()
        metrics.record_intent(ActionType.PLACE)
        metrics.set_orders_open("BTCUSDT", OrderSide.BUY, 2)

        result = metrics.get_metrics()

        assert "grinder_intents_total" in result
        assert "grinder_exec_events_total" in result
        assert "grinder_orders_open" in result
        assert result["grinder_intents_total"]["PLACE"] == 1
        assert result["grinder_orders_open"]["BTCUSDT:BUY"] == 2

    def test_reset_metrics(self) -> None:
        """Test resetting all metrics."""
        metrics = ExecutionMetrics()
        metrics.record_intent(ActionType.PLACE)
        metrics.set_orders_open("BTCUSDT", OrderSide.BUY, 2)

        metrics.reset()

        assert metrics.intents_total == {}
        assert metrics.exec_events_total == {}
        assert metrics.orders_open == {}


# --- Tests: Symbol Filtering ---


class TestSymbolFiltering:
    """Tests for symbol-specific order filtering."""

    def test_only_processes_matching_symbol(
        self,
        engine: ExecutionEngine,
        pause_plan: GridPlan,
    ) -> None:
        """Test only orders for matching symbol are processed."""
        state = ExecutionState(
            open_orders={
                "btc1": OrderRecord(
                    order_id="btc1",
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("49000"),
                    quantity=Decimal("0.1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
                "eth1": OrderRecord(
                    order_id="eth1",
                    symbol="ETHUSDT",  # Different symbol
                    side=OrderSide.BUY,
                    price=Decimal("3000"),
                    quantity=Decimal("1"),
                    state=OrderState.OPEN,
                    level_id=1,
                    created_ts=1000,
                ),
            },
            tick_counter=0,
        )

        result = engine.evaluate(pause_plan, "BTCUSDT", state, ts=2000)

        # Only BTCUSDT order should be cancelled
        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1
        assert cancel_actions[0].order_id == "btc1"


# --- Tests: M7-05 Symbol Qty Constraints ---


class TestFloorToStep:
    """Tests for floor_to_step helper (M7-05, ADR-059)."""

    def test_floor_to_step_rounds_down(self) -> None:
        """Test floor_to_step floors to step size (never rounds up)."""
        # 0.12345 floored to step 0.001 = 0.123
        result = floor_to_step(Decimal("0.12345"), Decimal("0.001"))
        assert result == Decimal("0.123")

        # 0.12399 floored to step 0.001 = 0.123 (not 0.124)
        result = floor_to_step(Decimal("0.12399"), Decimal("0.001"))
        assert result == Decimal("0.123")

    def test_floor_to_step_exact_multiple(self) -> None:
        """Test floor_to_step returns exact value for multiples."""
        # 0.123 is exact multiple of 0.001
        result = floor_to_step(Decimal("0.123"), Decimal("0.001"))
        assert result == Decimal("0.123")

        # 0.5 is exact multiple of 0.1
        result = floor_to_step(Decimal("0.5"), Decimal("0.1"))
        assert result == Decimal("0.5")

    def test_floor_to_step_zero_step(self) -> None:
        """Test floor_to_step passes through with step_size=0."""
        result = floor_to_step(Decimal("0.12345"), Decimal("0"))
        assert result == Decimal("0.12345")


class TestApplyQtyConstraints:
    """Tests for ExecutionEngine._apply_qty_constraints (M7-05, ADR-059)."""

    def test_apply_constraints_below_min(self) -> None:
        """Test returns EXEC_QTY_BELOW_MIN_QTY when rounded qty < min_qty."""
        port = NoOpExchangePort()
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.01"),
            )
        }
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # 0.005 floored to 0.001 = 0.005, which is < 0.01
        rounded_qty, reason = engine._apply_qty_constraints(Decimal("0.005"), "BTCUSDT")

        assert rounded_qty == Decimal("0.005")
        assert reason == "EXEC_QTY_BELOW_MIN_QTY"

    def test_apply_constraints_above_min(self) -> None:
        """Test returns (rounded_qty, None) when qty >= min_qty."""
        port = NoOpExchangePort()
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.01"),
            )
        }
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # 0.02345 floored to 0.001 = 0.023, which is >= 0.01
        rounded_qty, reason = engine._apply_qty_constraints(Decimal("0.02345"), "BTCUSDT")

        assert rounded_qty == Decimal("0.023")
        assert reason is None

    def test_apply_constraints_no_constraints(self) -> None:
        """Test passes through when no constraints configured for symbol."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints={}, config=config)

        qty, reason = engine._apply_qty_constraints(Decimal("0.00001"), "BTCUSDT")

        assert qty == Decimal("0.00001")
        assert reason is None


class TestEngineSkipsOrderBelowMin:
    """Integration test for order skipping (M7-05, ADR-059)."""

    def test_engine_skips_order_below_min(self) -> None:
        """Test engine emits ORDER_SKIPPED event when qty below min."""
        port = NoOpExchangePort()
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.1"),  # High min to trigger skip
            )
        }
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # Plan with very small qty that will be below min
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.05")],  # Below min_qty of 0.1
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Should have no placed orders
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2  # 2 actions generated but...

        # Should have skipped events for both orders
        skipped_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skipped_events) == 2

        # Verify event details
        for event in skipped_events:
            assert event.details["reason"] == "EXEC_QTY_BELOW_MIN_QTY"
            assert event.details["original_qty"] == "0.050"
            assert Decimal(event.details["rounded_qty"]) < Decimal("0.1")

        # State should have no open orders (all skipped)
        assert len(result.state.open_orders) == 0


# --- Tests: M7-07 Constraints Config Flag ---


class TestConstraintsEnabledFlag:
    """Tests for ExecutionEngineConfig.constraints_enabled (M7-07, ADR-061)."""

    def test_constraints_disabled_by_default(self) -> None:
        """Test constraints are NOT applied when config is default (disabled)."""
        port = NoOpExchangePort()
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.1"),  # High min that would trigger skip if enabled
            )
        }
        # No config = default = constraints_enabled=False
        engine = ExecutionEngine(port=port, symbol_constraints=constraints)

        # Plan with qty below min_qty
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.05")],  # Would be skipped if constraints enabled
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # With constraints disabled, orders should be placed (no skipping)
        assert len(result.state.open_orders) == 2
        skipped_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skipped_events) == 0

    def test_constraints_disabled_explicit(self) -> None:
        """Test constraints NOT applied when explicitly disabled."""
        port = NoOpExchangePort()
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.1"),
            )
        }
        config = ExecutionEngineConfig(constraints_enabled=False)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # _apply_qty_constraints should pass through
        qty, reason = engine._apply_qty_constraints(Decimal("0.05"), "BTCUSDT")

        assert qty == Decimal("0.05")  # Not rounded
        assert reason is None  # Not rejected

    def test_constraints_enabled_applies_constraints(self) -> None:
        """Test constraints ARE applied when explicitly enabled."""
        port = NoOpExchangePort()
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.1"),
            )
        }
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, symbol_constraints=constraints, config=config)

        # _apply_qty_constraints should round and reject
        qty, reason = engine._apply_qty_constraints(Decimal("0.05"), "BTCUSDT")

        assert qty == Decimal("0.050")  # Rounded to step
        assert reason == "EXEC_QTY_BELOW_MIN_QTY"  # Rejected

    def test_backward_compatible_no_constraints_no_config(self) -> None:
        """Test backward compatibility: no constraints, no config = pass-through."""
        port = NoOpExchangePort()
        engine = ExecutionEngine(port=port)

        # Plan should work exactly as before
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.001")],  # Valid qty for default precision
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Orders placed with original qty (only basic precision rounding)
        assert len(result.state.open_orders) == 2


class TestConstraintProviderLazyLoading:
    """Tests for constraint_provider lazy loading (M7-07, ADR-061)."""

    def test_lazy_load_from_provider(self) -> None:
        """Test constraints are lazy loaded from provider when needed."""
        # Create provider from fixture
        fixture_path = (
            Path(__file__).parent.parent
            / "fixtures"
            / "exchange_info"
            / "binance_futures_usdt.json"
        )
        provider = ConstraintProvider.from_cache(fixture_path)

        # Engine with provider and constraints enabled
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(port=port, config=config, constraint_provider=provider)

        # Constraints should not be loaded yet (lazy)
        assert engine._constraints_loaded is False

        # Plan that triggers constraint check
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.0015")],  # Will floor to 0.001
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=1000)

        # Constraints should now be loaded
        assert engine._constraints_loaded is True

        # Qty should be floored to step_size (0.001)
        placed_orders = list(result.state.open_orders.values())
        assert len(placed_orders) == 2
        for order in placed_orders:
            assert order.quantity == Decimal("0.001")

    def test_symbol_constraints_takes_precedence_over_provider(self) -> None:
        """Test symbol_constraints dict takes precedence over provider."""
        # Create provider from fixture (has BTC step_size=0.001)
        fixture_path = (
            Path(__file__).parent.parent
            / "fixtures"
            / "exchange_info"
            / "binance_futures_usdt.json"
        )
        provider = ConstraintProvider.from_cache(fixture_path)

        # Create engine with both direct constraints AND provider
        # Direct constraints use step_size=0.01 (different from provider's 0.001)
        port = NoOpExchangePort()
        direct_constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.01"),  # Different from fixture
                min_qty=Decimal("0.01"),
            )
        }
        config = ExecutionEngineConfig(constraints_enabled=True)
        engine = ExecutionEngine(
            port=port,
            symbol_constraints=direct_constraints,
            config=config,
            constraint_provider=provider,
        )

        # Constraints already loaded (from direct dict)
        assert engine._constraints_loaded is True

        # Should use direct constraints, not provider
        qty, _reason = engine._apply_qty_constraints(Decimal("0.025"), "BTCUSDT")
        assert qty == Decimal("0.02")  # Floored to 0.01 step, not 0.001


class TestL2ExecutionGuard:
    """Tests for L2 execution guard (M7-09, ADR-062)."""

    @pytest.fixture
    def fresh_l2_snapshot(self) -> L2FeatureSnapshot:
        """L2 snapshot with good conditions (low impact, sufficient depth)."""
        return L2FeatureSnapshot(
            ts_ms=1000,
            symbol="BTCUSDT",
            venue="binance_futures",
            depth=5,
            depth_bid_qty_topN=Decimal("100"),
            depth_ask_qty_topN=Decimal("100"),
            depth_imbalance_topN_bps=0,
            impact_buy_topN_bps=10,  # Low impact
            impact_sell_topN_bps=10,
            impact_buy_topN_insufficient_depth=0,
            impact_sell_topN_insufficient_depth=0,
            wall_bid_score_topN_x1000=1000,
            wall_ask_score_topN_x1000=1000,
            qty_ref=Decimal("1"),
        )

    @pytest.fixture
    def stale_l2_snapshot(self) -> L2FeatureSnapshot:
        """L2 snapshot that will be stale (old ts_ms)."""
        return L2FeatureSnapshot(
            ts_ms=0,  # Very old
            symbol="BTCUSDT",
            venue="binance_futures",
            depth=5,
            depth_bid_qty_topN=Decimal("100"),
            depth_ask_qty_topN=Decimal("100"),
            depth_imbalance_topN_bps=0,
            impact_buy_topN_bps=10,
            impact_sell_topN_bps=10,
            impact_buy_topN_insufficient_depth=0,
            impact_sell_topN_insufficient_depth=0,
            wall_bid_score_topN_x1000=1000,
            wall_ask_score_topN_x1000=1000,
            qty_ref=Decimal("1"),
        )

    @pytest.fixture
    def insufficient_depth_buy_snapshot(self) -> L2FeatureSnapshot:
        """L2 snapshot with insufficient buy depth."""
        return L2FeatureSnapshot(
            ts_ms=1000,
            symbol="BTCUSDT",
            venue="binance_futures",
            depth=5,
            depth_bid_qty_topN=Decimal("100"),
            depth_ask_qty_topN=Decimal("10"),
            depth_imbalance_topN_bps=0,
            impact_buy_topN_bps=500,  # Sentinel value
            impact_sell_topN_bps=10,
            impact_buy_topN_insufficient_depth=1,  # Insufficient
            impact_sell_topN_insufficient_depth=0,
            wall_bid_score_topN_x1000=1000,
            wall_ask_score_topN_x1000=1000,
            qty_ref=Decimal("1"),
        )

    @pytest.fixture
    def high_impact_sell_snapshot(self) -> L2FeatureSnapshot:
        """L2 snapshot with high sell impact."""
        return L2FeatureSnapshot(
            ts_ms=1000,
            symbol="BTCUSDT",
            venue="binance_futures",
            depth=5,
            depth_bid_qty_topN=Decimal("100"),
            depth_ask_qty_topN=Decimal("100"),
            depth_imbalance_topN_bps=0,
            impact_buy_topN_bps=10,
            impact_sell_topN_bps=100,  # High impact (> threshold of 50)
            impact_buy_topN_insufficient_depth=0,
            impact_sell_topN_insufficient_depth=0,
            wall_bid_score_topN_x1000=1000,
            wall_ask_score_topN_x1000=1000,
            qty_ref=Decimal("1"),
        )

    def test_l2_guard_disabled_is_noop(self, fresh_l2_snapshot: L2FeatureSnapshot) -> None:
        """Test L2 guard is no-op when disabled (default)."""
        port = NoOpExchangePort()
        # Default config has l2_execution_guard_enabled=False
        engine = ExecutionEngine(
            port=port,
            l2_features={"BTCUSDT": fresh_l2_snapshot},
        )

        # Guard should return False (don't skip)
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.BUY, ts=1000)

        assert skip is False
        assert reason is None

    def test_l2_guard_enabled_missing_features_pass_through(self) -> None:
        """Test L2 guard passes through when no features provided (safe rollout)."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(l2_execution_guard_enabled=True)
        # No l2_features provided
        engine = ExecutionEngine(port=port, config=config)

        # Guard should return False (pass-through, don't skip)
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.BUY, ts=1000)

        assert skip is False
        assert reason is None

    def test_l2_guard_stale_skips_place(self, stale_l2_snapshot: L2FeatureSnapshot) -> None:
        """Test L2 guard skips PLACE when L2 data is stale."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(
            l2_execution_guard_enabled=True,
            l2_execution_max_age_ms=1500,
        )
        engine = ExecutionEngine(
            port=port,
            config=config,
            l2_features={"BTCUSDT": stale_l2_snapshot},  # ts_ms=0
        )

        # ts=2000 means snapshot age = 2000 - 0 = 2000ms > max_age_ms=1500
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.BUY, ts=2000)

        assert skip is True
        assert reason == "EXEC_L2_STALE"

    def test_l2_guard_insufficient_depth_buy_skips(
        self, insufficient_depth_buy_snapshot: L2FeatureSnapshot
    ) -> None:
        """Test L2 guard skips BUY when buy depth is insufficient."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(l2_execution_guard_enabled=True)
        engine = ExecutionEngine(
            port=port,
            config=config,
            l2_features={"BTCUSDT": insufficient_depth_buy_snapshot},
        )

        # BUY order should be skipped
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.BUY, ts=1000)

        assert skip is True
        assert reason == "EXEC_L2_INSUFFICIENT_DEPTH_BUY"

        # SELL order should NOT be skipped (sell depth is fine)
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.SELL, ts=1000)

        assert skip is False
        assert reason is None

    def test_l2_guard_impact_high_sell_skips(
        self, high_impact_sell_snapshot: L2FeatureSnapshot
    ) -> None:
        """Test L2 guard skips SELL when sell impact is too high."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(
            l2_execution_guard_enabled=True,
            l2_execution_impact_threshold_bps=50,  # Threshold
        )
        engine = ExecutionEngine(
            port=port,
            config=config,
            l2_features={"BTCUSDT": high_impact_sell_snapshot},  # sell impact=100
        )

        # SELL order should be skipped (100 >= 50 threshold)
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.SELL, ts=1000)

        assert skip is True
        assert reason == "EXEC_L2_IMPACT_SELL_HIGH"

        # BUY order should NOT be skipped (buy impact=10 < 50)
        skip, reason = engine._apply_l2_guard("BTCUSDT", OrderSide.BUY, ts=1000)

        assert skip is False
        assert reason is None

    def test_l2_guard_applies_only_to_place_not_cancel(
        self, stale_l2_snapshot: L2FeatureSnapshot
    ) -> None:
        """Test L2 guard does NOT affect CANCEL actions (only PLACE)."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(l2_execution_guard_enabled=True)
        engine = ExecutionEngine(
            port=port,
            config=config,
            l2_features={"BTCUSDT": stale_l2_snapshot},
        )

        # Create state with an existing order
        existing_order = OrderRecord(
            order_id="order-1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            state=OrderState.OPEN,
            level_id=1,
            created_ts=500,
        )
        state = ExecutionState(
            open_orders={"order-1": existing_order},
            last_plan_digest="",
            tick_counter=0,
        )

        # PAUSE plan cancels all orders
        pause_plan = GridPlan(
            mode=GridMode.PAUSE,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.1")],
            reason_codes=["PAUSE"],
        )

        # Even with stale L2 data, CANCEL should work (L2 guard only affects PLACE)
        result = engine.evaluate(pause_plan, "BTCUSDT", state, ts=2000)

        # Order should be cancelled
        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 1

        # No L2-related skip events
        l2_skips = [
            e
            for e in result.events
            if e.event_type == "ORDER_SKIPPED" and "L2" in e.details.get("reason", "")
        ]
        assert len(l2_skips) == 0

    def test_l2_guard_runs_before_qty_constraints(
        self, stale_l2_snapshot: L2FeatureSnapshot
    ) -> None:
        """Test L2 guard runs BEFORE qty constraints (order matters)."""
        port = NoOpExchangePort()
        config = ExecutionEngineConfig(
            l2_execution_guard_enabled=True,
            constraints_enabled=True,
        )
        # Stale L2 data + constraints that would also reject
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"),
                min_qty=Decimal("1.0"),  # Would reject qty=0.1
            )
        }
        engine = ExecutionEngine(
            port=port,
            config=config,
            symbol_constraints=constraints,
            l2_features={"BTCUSDT": stale_l2_snapshot},
        )

        # Plan with qty that would be rejected by both guards
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=1,
            levels_down=1,
            size_schedule=[Decimal("0.1")],  # < min_qty=1.0
            reason_codes=["TEST"],
        )

        empty_state = ExecutionState(open_orders={}, last_plan_digest="", tick_counter=0)
        result = engine.evaluate(plan, "BTCUSDT", empty_state, ts=2000)

        # L2 guard should fire first (EXEC_L2_STALE), not qty constraint
        skip_events = [e for e in result.events if e.event_type == "ORDER_SKIPPED"]
        assert len(skip_events) == 2  # Both BUY and SELL skipped
        for event in skip_events:
            assert event.details["reason"] == "EXEC_L2_STALE"  # L2 guard, not qty


class TestRoundPriceTickSize:
    """Tests for _round_price with tick_size from SymbolConstraints."""

    def test_round_price_with_tick_size_010(self) -> None:
        """BTCUSDT futures: tick_size=0.10, price floors to 0.10 multiple."""
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.10")
            )
        }
        engine = ExecutionEngine(
            port=NoOpExchangePort(),
            symbol_constraints=constraints,
            config=ExecutionEngineConfig(constraints_enabled=True),
        )
        # 85123.45 → floor to 85123.40 (nearest 0.10 below)
        result = engine._round_price(Decimal("85123.45"), "BTCUSDT")
        assert result == Decimal("85123.4")

    def test_round_price_with_tick_size_001(self) -> None:
        """ETHUSDT: tick_size=0.01, price floors to 0.01 multiple."""
        constraints = {
            "ETHUSDT": SymbolConstraints(
                step_size=Decimal("0.01"), min_qty=Decimal("0.01"), tick_size=Decimal("0.01")
            )
        }
        engine = ExecutionEngine(
            port=NoOpExchangePort(),
            symbol_constraints=constraints,
            config=ExecutionEngineConfig(constraints_enabled=True),
        )
        result = engine._round_price(Decimal("3456.789"), "ETHUSDT")
        assert result == Decimal("3456.78")

    def test_round_price_no_tick_size_falls_back(self) -> None:
        """No tick_size → falls back to price_precision=2."""
        engine = ExecutionEngine(port=NoOpExchangePort(), price_precision=2)
        result = engine._round_price(Decimal("85123.456"), "BTCUSDT")
        assert result == Decimal("85123.45")

    def test_round_price_constraints_disabled_ignores_tick_size(self) -> None:
        """constraints_enabled=False → ignores tick_size, uses price_precision."""
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.10")
            )
        }
        engine = ExecutionEngine(
            port=NoOpExchangePort(),
            symbol_constraints=constraints,
            config=ExecutionEngineConfig(constraints_enabled=False),
        )
        result = engine._round_price(Decimal("85123.45"), "BTCUSDT")
        # Falls back to price_precision=2 → no tick_size rounding
        assert result == Decimal("85123.45")

    def test_round_price_unknown_symbol_falls_back(self) -> None:
        """Unknown symbol → falls back to price_precision=2."""
        constraints = {
            "BTCUSDT": SymbolConstraints(
                step_size=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.10")
            )
        }
        engine = ExecutionEngine(
            port=NoOpExchangePort(),
            symbol_constraints=constraints,
            config=ExecutionEngineConfig(constraints_enabled=True),
        )
        result = engine._round_price(Decimal("85123.45"), "XYZUSDT")
        assert result == Decimal("85123.45")  # price_precision=2 fallback
