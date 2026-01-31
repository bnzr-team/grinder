"""Deterministic fill simulation for paper trading.

Fill logic:
- All PLACE orders fill immediately at their limit price
- No slippage, no partial fills (paper trading simplification)
- Fill events are fully deterministic given the same inputs

This module is intentionally simple for v0. Future versions may add:
- Simulated slippage based on order size vs liquidity
- Partial fills
- Fill probability based on price distance from mid
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
) -> list[Fill]:
    """Simulate fills for PLACE actions.

    In paper trading v0, all PLACE orders fill immediately at their limit price.
    CANCEL actions do not generate fills.

    Args:
        ts: Current timestamp
        symbol: Trading symbol
        actions: List of action dicts from ExecutionEngine

    Returns:
        List of Fill objects for PLACE actions
    """
    fills: list[Fill] = []

    for idx, action in enumerate(actions):
        # ExecutionAction uses action_type, not type
        if action.get("action_type") != "PLACE":
            continue

        # Generate deterministic order_id if not present
        order_id = action.get("order_id")
        if order_id is None:
            # Deterministic ID based on ts, symbol, index, side, price
            order_id = f"paper_{ts}_{symbol}_{idx}_{action['side']}_{action['price']}"

        fill = Fill(
            ts=ts,
            symbol=symbol,
            side=action["side"],
            price=Decimal(str(action["price"])),
            quantity=Decimal(str(action["quantity"])),
            order_id=order_id,
        )
        fills.append(fill)

    return fills
