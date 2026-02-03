"""Deterministic fill simulation for paper trading.

Fill logic (v1 crossing/touch model):
- LIMIT BUY fills if mid_price <= limit_price (price crosses or touches)
- LIMIT SELL fills if mid_price >= limit_price (price crosses or touches)
- No slippage, no partial fills (paper trading simplification)
- Fill events are fully deterministic given the same inputs

v0 behavior (instant fills for all PLACE orders) is preserved via
fill_mode="instant" for backward compatibility. Default is now "crossing".

Future versions may add:
- Simulated slippage based on order size vs liquidity
- Partial fills (PR-ASM-P0-02+)
- L2-based impact model
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


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
