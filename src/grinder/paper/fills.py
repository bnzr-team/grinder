"""Deterministic fill simulation for paper trading.

Fill logic (v1 crossing/touch model):
- LIMIT BUY fills if mid_price <= limit_price (price crosses or touches)
- LIMIT SELL fills if mid_price >= limit_price (price crosses or touches)
- No slippage, no partial fills (paper trading simplification)
- Fill events are fully deterministic given the same inputs

v0 behavior (instant fills for all PLACE orders) is preserved via
fill_mode="instant" for backward compatibility. Default is now "crossing".

v0.1 tick-delay model (LC-03):
- Orders remain OPEN for N ticks before becoming fill-eligible
- fill_after_ticks=0: instant/crossing (current behavior)
- fill_after_ticks=1: fill on next tick after placement (if price crosses)
- Fully deterministic: same ticks → same fills

Future versions may add:
- Simulated slippage based on order size vs liquidity
- Partial fills (PR-ASM-P0-02+)
- L2-based impact model
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.execution.types import OrderRecord


@dataclass(frozen=True)
class Fill:
    """A simulated fill event.

    Attributes:
        ts: Timestamp of the fill (same as order timestamp)
        symbol: Trading symbol
        side: "BUY" or "SELL"
        price: Fill price (same as order limit price for paper trading)
        quantity: Fill quantity
        order_id: Reference to the order that was filled
    """

    ts: int
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    order_id: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "side": self.side,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "order_id": self.order_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fill:
        """Create from dict."""
        return cls(
            ts=d["ts"],
            symbol=d["symbol"],
            side=d["side"],
            price=Decimal(d["price"]),
            quantity=Decimal(d["quantity"]),
            order_id=d["order_id"],
        )


def simulate_fills(
    ts: int,
    symbol: str,
    actions: list[dict[str, Any]],
    mid_price: Decimal | None = None,
    fill_mode: str = "crossing",
) -> list[Fill]:
    """Simulate fills for PLACE actions using crossing/touch model.

    v1 crossing/touch model (default):
    - LIMIT BUY fills if mid_price <= limit_price
    - LIMIT SELL fills if mid_price >= limit_price

    v0 instant mode (fill_mode="instant"):
    - All PLACE orders fill immediately at their limit price

    CANCEL actions never generate fills.

    Args:
        ts: Current timestamp
        symbol: Trading symbol
        actions: List of action dicts from ExecutionEngine
        mid_price: Current mid price for crossing check (required for crossing mode)
        fill_mode: "crossing" (default) or "instant" (v0 backward compat)

    Returns:
        List of Fill objects for orders that would fill
    """
    fills: list[Fill] = []

    for idx, action in enumerate(actions):
        # ExecutionAction uses action_type, not type
        if action.get("action_type") != "PLACE":
            continue

        limit_price = Decimal(str(action["price"]))
        side = action["side"]

        # Check if order would fill based on fill_mode
        if fill_mode == "crossing" and mid_price is not None:
            # v1: crossing/touch model
            # BUY fills if mid <= limit (price has come down to our buy level)
            # SELL fills if mid >= limit (price has come up to our sell level)
            if side == "BUY" and mid_price > limit_price:
                continue  # No fill - price hasn't reached our buy level
            if side == "SELL" and mid_price < limit_price:
                continue  # No fill - price hasn't reached our sell level
        # else: instant mode (v0) - all orders fill

        # Generate deterministic order_id if not present
        order_id = action.get("order_id")
        if order_id is None:
            # Deterministic ID based on ts, symbol, index, side, price
            order_id = f"paper_{ts}_{symbol}_{idx}_{side}_{action['price']}"

        fill = Fill(
            ts=ts,
            symbol=symbol,
            side=side,
            price=limit_price,
            quantity=Decimal(str(action["quantity"])),
            order_id=order_id,
        )
        fills.append(fill)

    return fills


@dataclass
class PendingFillResult:
    """Result of checking pending orders for fills.

    Attributes:
        fills: List of Fill objects for orders that filled
        filled_order_ids: Set of order_ids that were filled (for state update)
    """

    fills: list[Fill]
    filled_order_ids: set[str]


def check_pending_fills(
    ts: int,
    open_orders: list[OrderRecord],
    mid_price: Decimal,
    current_tick: int,
    fill_after_ticks: int = 1,
) -> PendingFillResult:
    """Check pending OPEN orders for fill eligibility (LC-03 tick-delay model).

    An order fills when BOTH conditions are met:
    1. Tick eligibility: current_tick - placed_tick >= fill_after_ticks
    2. Price crossing: BUY if mid <= limit, SELL if mid >= limit

    This function is deterministic: same inputs → same fills.

    Args:
        ts: Current timestamp for fill events
        open_orders: List of OrderRecord objects in OPEN state
        mid_price: Current mid price for crossing check
        current_tick: Current snapshot counter
        fill_after_ticks: Minimum ticks before order can fill (default 1)

    Returns:
        PendingFillResult with fills and set of filled order_ids
    """
    fills: list[Fill] = []
    filled_order_ids: set[str] = set()

    # Sort by order_id for deterministic processing order
    sorted_orders = sorted(open_orders, key=lambda o: o.order_id)

    for order in sorted_orders:
        # Skip orders not yet tick-eligible
        ticks_since_placed = current_tick - order.placed_tick
        if ticks_since_placed < fill_after_ticks:
            continue

        # Check price crossing condition
        # BUY fills if mid <= limit (price came down to our level)
        # SELL fills if mid >= limit (price came up to our level)
        side_str = order.side.value
        if side_str == "BUY" and mid_price > order.price:
            continue  # Price hasn't reached our buy level
        if side_str == "SELL" and mid_price < order.price:
            continue  # Price hasn't reached our sell level

        # Order fills!
        fill = Fill(
            ts=ts,
            symbol=order.symbol,
            side=side_str,
            price=order.price,
            quantity=order.quantity,
            order_id=order.order_id,
        )
        fills.append(fill)
        filled_order_ids.add(order.order_id)

    return PendingFillResult(fills=fills, filled_order_ids=filled_order_ids)
