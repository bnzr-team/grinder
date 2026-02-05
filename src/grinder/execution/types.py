"""Execution types for order management.

These are internal types used by the execution engine.
External contracts (OrderIntent, Decision) are in grinder.contracts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from grinder.core import OrderSide, OrderState


class ActionType(Enum):
    """Execution action types."""

    PLACE = "PLACE"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"
    NOOP = "NOOP"


@dataclass
class OrderRecord:
    """Internal order tracking record.

    This represents an order tracked by the execution engine.

    Attributes:
        order_id: Unique order identifier
        symbol: Trading symbol
        side: BUY or SELL
        price: Limit price
        quantity: Order quantity
        state: Current order state (OPEN, FILLED, CANCELLED, etc.)
        level_id: Grid level index
        created_ts: Creation timestamp
        placed_tick: Snapshot counter when order was placed (for tick-delay fills)
    """

    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    state: OrderState
    level_id: int  # Grid level index
    created_ts: int
    placed_tick: int = 0  # Snapshot counter when placed (LC-03)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "state": self.state.value,
            "level_id": self.level_id,
            "created_ts": self.created_ts,
            "placed_tick": self.placed_tick,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OrderRecord:
        """Create from dict."""
        return cls(
            order_id=d["order_id"],
            symbol=d["symbol"],
            side=OrderSide(d["side"]),
            price=Decimal(d["price"]),
            quantity=Decimal(d["quantity"]),
            state=OrderState(d["state"]),
            level_id=d["level_id"],
            created_ts=d["created_ts"],
            placed_tick=d.get("placed_tick", 0),  # Backward compat
        )


@dataclass
class ExecutionAction:
    """Single execution action (place/cancel/replace)."""

    action_type: ActionType
    order_id: str | None = None  # For cancel/replace
    symbol: str = ""
    side: OrderSide | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    level_id: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "action_type": self.action_type.value,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value if self.side else None,
            "price": str(self.price) if self.price else None,
            "quantity": str(self.quantity) if self.quantity else None,
            "level_id": self.level_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExecutionAction:
        """Create from dict."""
        return cls(
            action_type=ActionType(d["action_type"]),
            order_id=d.get("order_id"),
            symbol=d.get("symbol", ""),
            side=OrderSide(d["side"]) if d.get("side") else None,
            price=Decimal(d["price"]) if d.get("price") else None,
            quantity=Decimal(d["quantity"]) if d.get("quantity") else None,
            level_id=d.get("level_id", 0),
            reason=d.get("reason", ""),
        )


@dataclass
class ExecutionEvent:
    """Event emitted by execution engine for logging/metrics."""

    ts: int
    event_type: str  # PLACE_ORDER, CANCEL_ORDER, RECONCILE, etc.
    symbol: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "details": self.details,
        }

    def to_json(self) -> str:
        """Serialize to JSON string (deterministic)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass
class ExecutionState:
    """State maintained by execution engine.

    Tracks open orders and last plan digest for reconciliation.
    """

    open_orders: dict[str, OrderRecord] = field(default_factory=dict)
    last_plan_digest: str = ""
    tick_counter: int = 0  # For deterministic order ID generation

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "open_orders": {k: v.to_dict() for k, v in self.open_orders.items()},
            "last_plan_digest": self.last_plan_digest,
            "tick_counter": self.tick_counter,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExecutionState:
        """Create from dict."""
        return cls(
            open_orders={k: OrderRecord.from_dict(v) for k, v in d.get("open_orders", {}).items()},
            last_plan_digest=d.get("last_plan_digest", ""),
            tick_counter=d.get("tick_counter", 0),
        )
