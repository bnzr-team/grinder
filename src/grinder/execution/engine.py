"""Execution engine for grid order management.

The ExecutionEngine:
- Takes GridPlan as input
- Computes required order levels
- Reconciles with current open orders
- Generates execution actions (place/cancel)
- Maintains deterministic state for replay
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from grinder.core import GridMode, OrderSide, OrderState, ResetAction
from grinder.execution.types import (
    ActionType,
    ExecutionAction,
    ExecutionEvent,
    ExecutionState,
    OrderRecord,
)

if TYPE_CHECKING:
    from grinder.execution.port import ExchangePort
    from grinder.policies.base import GridPlan


@dataclass
class GridLevel:
    """Computed grid level for order placement."""

    level_id: int
    side: OrderSide
    price: Decimal
    quantity: Decimal

    def key(self) -> str:
        """Unique key for matching."""
        return f"{self.side.value}:{self.level_id}"


@dataclass
class ExecutionResult:
    """Result of execution engine evaluation."""

    actions: list[ExecutionAction]
    events: list[ExecutionEvent]
    state: ExecutionState
    plan_digest: str


class ExecutionEngine:
    """Grid order execution engine.

    Responsibilities:
    - Compute grid levels from GridPlan
    - Reconcile current orders with desired grid
    - Generate execution actions
    - Emit events for logging/metrics
    - Maintain deterministic state
    """

    def __init__(
        self,
        port: ExchangePort,
        price_precision: int = 2,
        quantity_precision: int = 3,
    ) -> None:
        """Initialize execution engine.

        Args:
            port: Exchange port for order operations
            price_precision: Decimal places for price rounding
            quantity_precision: Decimal places for quantity rounding
        """
        self._port = port
        self._price_precision = price_precision
        self._quantity_precision = quantity_precision

    def _round_price(self, price: Decimal) -> Decimal:
        """Round price to configured precision."""
        quantize_str = "0." + "0" * self._price_precision
        return price.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    def _round_quantity(self, qty: Decimal) -> Decimal:
        """Round quantity to configured precision."""
        quantize_str = "0." + "0" * self._quantity_precision
        return qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    def _compute_plan_digest(self, plan: GridPlan) -> str:
        """Compute deterministic digest of a plan."""
        plan_dict = {
            "mode": plan.mode.value,
            "center_price": str(plan.center_price),
            "spacing_bps": plan.spacing_bps,
            "levels_up": plan.levels_up,
            "levels_down": plan.levels_down,
            "size_schedule": [str(s) for s in plan.size_schedule],
            "skew_bps": plan.skew_bps,
            "reset_action": plan.reset_action.value,
        }
        content = json.dumps(plan_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _compute_grid_levels(self, plan: GridPlan, symbol: str) -> list[GridLevel]:  # noqa: ARG002
        """Compute grid levels from plan.

        Grid level computation:
        - center_price is the reference point
        - levels_up: SELL orders above center
        - levels_down: BUY orders below center
        - spacing_bps determines distance between levels
        - size_schedule provides quantity per level
        """
        if plan.mode in (GridMode.PAUSE, GridMode.EMERGENCY):
            return []

        levels: list[GridLevel] = []
        center = plan.center_price

        # Apply skew to center
        skew_factor = Decimal(str(1 + plan.skew_bps / 10000))
        skewed_center = center * skew_factor

        # Spacing factor
        spacing_factor = Decimal(str(1 + plan.spacing_bps / 10000))
        spacing_factor_down = Decimal(str(1 - plan.spacing_bps / 10000))

        # Generate SELL levels (above center) if not UNI_LONG mode
        # Price formula: center * (1 + spacing_bps/10000)^i
        if plan.mode not in (GridMode.UNI_LONG,):
            for i in range(1, plan.levels_up + 1):
                price = skewed_center
                for _ in range(i):
                    price = price * spacing_factor

                price = self._round_price(price)
                size_idx = min(i - 1, len(plan.size_schedule) - 1)
                quantity = self._round_quantity(plan.size_schedule[size_idx])

                levels.append(
                    GridLevel(
                        level_id=i,
                        side=OrderSide.SELL,
                        price=price,
                        quantity=quantity,
                    )
                )

        # Generate BUY levels (below center) if not UNI_SHORT mode
        # Price formula: center * (1 - spacing_bps/10000)^i
        if plan.mode not in (GridMode.UNI_SHORT,):
            for i in range(1, plan.levels_down + 1):
                price = skewed_center
                for _ in range(i):
                    price = price * spacing_factor_down

                price = self._round_price(price)
                size_idx = min(i - 1, len(plan.size_schedule) - 1)
                quantity = self._round_quantity(plan.size_schedule[size_idx])

                levels.append(
                    GridLevel(
                        level_id=i,
                        side=OrderSide.BUY,
                        price=price,
                        quantity=quantity,
                    )
                )

        return levels

    def _find_matching_order(
        self,
        level: GridLevel,
        open_orders: list[OrderRecord],
    ) -> OrderRecord | None:
        """Find an open order that matches the grid level."""
        for order in open_orders:
            if order.side == level.side and order.level_id == level.level_id:
                return order
        return None

    def _orders_match(self, level: GridLevel, order: OrderRecord) -> bool:
        """Check if order matches level exactly."""
        return order.price == level.price and order.quantity == level.quantity

    def evaluate(
        self,
        plan: GridPlan,
        symbol: str,
        state: ExecutionState,
        ts: int,
    ) -> ExecutionResult:
        """Evaluate plan and generate execution actions.

        Args:
            plan: GridPlan from policy
            symbol: Trading symbol
            state: Current execution state
            ts: Current timestamp

        Returns:
            ExecutionResult with actions, events, and updated state
        """
        actions: list[ExecutionAction] = []
        events: list[ExecutionEvent] = []

        # Compute plan digest
        plan_digest = self._compute_plan_digest(plan)

        # Get current open orders for symbol
        current_orders = [
            order
            for order in state.open_orders.values()
            if order.symbol == symbol
            and order.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
        ]

        # Handle PAUSE/EMERGENCY mode - cancel all
        if plan.mode in (GridMode.PAUSE, GridMode.EMERGENCY):
            for order in current_orders:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=order.order_id,
                        symbol=symbol,
                        reason=f"MODE_{plan.mode.value}",
                    )
                )
            events.append(
                ExecutionEvent(
                    ts=ts,
                    event_type=f"CANCEL_ALL_{plan.mode.value}",
                    symbol=symbol,
                    details={"cancelled_count": len(current_orders)},
                )
            )
            return self._apply_actions(actions, events, state, plan_digest, ts)

        # Handle HARD reset - cancel all, rebuild grid
        if plan.reset_action == ResetAction.HARD:
            # Cancel all existing orders
            for order in current_orders:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=order.order_id,
                        symbol=symbol,
                        reason="HARD_RESET",
                    )
                )

            # Compute new grid levels and place orders
            levels = self._compute_grid_levels(plan, symbol)
            for level in levels:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.PLACE,
                        symbol=symbol,
                        side=level.side,
                        price=level.price,
                        quantity=level.quantity,
                        level_id=level.level_id,
                        reason="HARD_RESET",
                    )
                )

            events.append(
                ExecutionEvent(
                    ts=ts,
                    event_type="HARD_RESET",
                    symbol=symbol,
                    details={
                        "cancelled_count": len(current_orders),
                        "placed_count": len(levels),
                    },
                )
            )
            return self._apply_actions(actions, events, state, plan_digest, ts)

        # Handle SOFT reset or NONE - reconcile
        levels = self._compute_grid_levels(plan, symbol)

        # Build sets for reconciliation
        level_keys = {level.key() for level in levels}
        order_keys = {f"{o.side.value}:{o.level_id}" for o in current_orders}

        # Cancel orders that don't match any level
        for order in current_orders:
            order_key = f"{order.side.value}:{order.level_id}"
            if order_key not in level_keys:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.CANCEL,
                        order_id=order.order_id,
                        symbol=symbol,
                        reason="RECONCILE_REMOVE",
                    )
                )
            else:
                # Check if order needs update (SOFT reset or price/qty mismatch)
                matching_level = next((lv for lv in levels if lv.key() == order_key), None)
                if (
                    matching_level
                    and not self._orders_match(matching_level, order)
                    and plan.reset_action == ResetAction.SOFT
                ):
                    # Cancel and replace
                    actions.append(
                        ExecutionAction(
                            action_type=ActionType.CANCEL,
                            order_id=order.order_id,
                            symbol=symbol,
                            reason="SOFT_RESET_REPLACE",
                        )
                    )
                    actions.append(
                        ExecutionAction(
                            action_type=ActionType.PLACE,
                            symbol=symbol,
                            side=matching_level.side,
                            price=matching_level.price,
                            quantity=matching_level.quantity,
                            level_id=matching_level.level_id,
                            reason="SOFT_RESET_REPLACE",
                        )
                    )

        # Place orders for missing levels
        for level in levels:
            if level.key() not in order_keys:
                actions.append(
                    ExecutionAction(
                        action_type=ActionType.PLACE,
                        symbol=symbol,
                        side=level.side,
                        price=level.price,
                        quantity=level.quantity,
                        level_id=level.level_id,
                        reason="RECONCILE_ADD",
                    )
                )

        # Record reconcile event
        placed_count = sum(1 for a in actions if a.action_type == ActionType.PLACE)
        cancelled_count = sum(1 for a in actions if a.action_type == ActionType.CANCEL)
        events.append(
            ExecutionEvent(
                ts=ts,
                event_type="RECONCILE",
                symbol=symbol,
                details={
                    "placed_count": placed_count,
                    "cancelled_count": cancelled_count,
                    "reset_action": plan.reset_action.value,
                },
            )
        )

        return self._apply_actions(actions, events, state, plan_digest, ts)

    def _apply_actions(
        self,
        actions: list[ExecutionAction],
        events: list[ExecutionEvent],
        state: ExecutionState,
        plan_digest: str,
        ts: int,
    ) -> ExecutionResult:
        """Apply actions to state and port."""
        new_orders = dict(state.open_orders)
        tick = state.tick_counter + 1

        for action in actions:
            if action.action_type == ActionType.CANCEL and action.order_id:
                # Update order state to cancelled
                if action.order_id in new_orders:
                    old_order = new_orders[action.order_id]
                    new_orders[action.order_id] = OrderRecord(
                        order_id=old_order.order_id,
                        symbol=old_order.symbol,
                        side=old_order.side,
                        price=old_order.price,
                        quantity=old_order.quantity,
                        state=OrderState.CANCELLED,
                        level_id=old_order.level_id,
                        created_ts=old_order.created_ts,
                    )
                self._port.cancel_order(action.order_id)

            elif action.action_type == ActionType.PLACE:
                if action.side and action.price and action.quantity:
                    order_id = self._port.place_order(
                        symbol=action.symbol,
                        side=action.side,
                        price=action.price,
                        quantity=action.quantity,
                        level_id=action.level_id,
                        ts=ts,
                    )
                    new_orders[order_id] = OrderRecord(
                        order_id=order_id,
                        symbol=action.symbol,
                        side=action.side,
                        price=action.price,
                        quantity=action.quantity,
                        state=OrderState.OPEN,
                        level_id=action.level_id,
                        created_ts=ts,
                    )

        new_state = ExecutionState(
            open_orders=new_orders,
            last_plan_digest=plan_digest,
            tick_counter=tick,
        )

        return ExecutionResult(
            actions=actions,
            events=events,
            state=new_state,
            plan_digest=plan_digest,
        )
