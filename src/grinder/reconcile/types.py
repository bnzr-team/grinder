"""Reconciliation types for expected/observed state comparison.

See ADR-042 for design decisions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from grinder.core import OrderSide, OrderState


class MismatchType(Enum):
    """Types of reconciliation mismatches.

    These values are STABLE and used as metric labels.
    DO NOT rename or remove values without updating metric contracts.
    """

    ORDER_MISSING_ON_EXCHANGE = "ORDER_MISSING_ON_EXCHANGE"
    ORDER_EXISTS_UNEXPECTED = "ORDER_EXISTS_UNEXPECTED"
    ORDER_STATUS_DIVERGENCE = "ORDER_STATUS_DIVERGENCE"
    POSITION_NONZERO_UNEXPECTED = "POSITION_NONZERO_UNEXPECTED"


@dataclass(frozen=True)
class ExpectedOrder:
    """Order we expect to exist on exchange.

    Source: Recorded when smoke harness calls place_order().
    Primary key: client_order_id

    Attributes:
        client_order_id: Our deterministic order ID (grinder_{symbol}_{...})
        symbol: Trading symbol
        side: BUY or SELL
        order_type: LIMIT (v0.1 only supports LIMIT)
        price: Limit price
        orig_qty: Original order quantity
        ts_created: Timestamp when we sent the order (ms)
        expected_status: What we expect the status to be (OPEN, FILLED, etc.)
    """

    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: str
    price: Decimal
    orig_qty: Decimal
    ts_created: int
    expected_status: OrderState = OrderState.OPEN

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type,
            "price": str(self.price),
            "orig_qty": str(self.orig_qty),
            "ts_created": self.ts_created,
            "expected_status": self.expected_status.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExpectedOrder:
        """Create from dict."""
        return cls(
            client_order_id=d["client_order_id"],
            symbol=d["symbol"],
            side=OrderSide(d["side"]),
            order_type=d["order_type"],
            price=Decimal(d["price"]),
            orig_qty=Decimal(d["orig_qty"]),
            ts_created=d["ts_created"],
            expected_status=OrderState(d.get("expected_status", "OPEN")),
        )

    def to_json(self) -> str:
        """Convert to deterministic JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ExpectedPosition:
    """Position we expect on exchange.

    For v0.1 (simple smoke harness): always expect position_amt = 0

    Attributes:
        symbol: Trading symbol
        expected_position_amt: Expected position (0 for v0.1)
        ts_updated: Last update timestamp
    """

    symbol: str
    expected_position_amt: Decimal = Decimal("0")
    ts_updated: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "expected_position_amt": str(self.expected_position_amt),
            "ts_updated": self.ts_updated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExpectedPosition:
        """Create from dict."""
        return cls(
            symbol=d["symbol"],
            expected_position_amt=Decimal(d.get("expected_position_amt", "0")),
            ts_updated=d.get("ts_updated", 0),
        )

    def to_json(self) -> str:
        """Convert to deterministic JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ObservedOrder:
    """Order observed from stream or REST snapshot.

    Source: FuturesOrderEvent from user-data stream, or REST /openOrders

    Attributes:
        client_order_id: Client order ID
        symbol: Trading symbol
        order_id: Binance numeric order ID
        side: BUY or SELL
        status: Current status from Binance
        price: Limit price
        orig_qty: Original quantity
        executed_qty: Filled quantity
        avg_price: Average fill price
        ts_observed: When we observed this state
        source: "stream" or "rest"
    """

    client_order_id: str
    symbol: str
    order_id: int
    side: OrderSide
    status: OrderState
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    ts_observed: int
    source: str = "stream"

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "side": self.side.value,
            "status": self.status.value,
            "price": str(self.price),
            "orig_qty": str(self.orig_qty),
            "executed_qty": str(self.executed_qty),
            "avg_price": str(self.avg_price),
            "ts_observed": self.ts_observed,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObservedOrder:
        """Create from dict."""
        return cls(
            client_order_id=d["client_order_id"],
            symbol=d["symbol"],
            order_id=d["order_id"],
            side=OrderSide(d["side"]),
            status=OrderState(d["status"]),
            price=Decimal(d["price"]),
            orig_qty=Decimal(d["orig_qty"]),
            executed_qty=Decimal(d["executed_qty"]),
            avg_price=Decimal(d["avg_price"]),
            ts_observed=d["ts_observed"],
            source=d.get("source", "stream"),
        )

    def to_json(self) -> str:
        """Convert to deterministic JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def is_terminal(self) -> bool:
        """Check if order is in terminal state (FILLED, CANCELLED, REJECTED, EXPIRED)."""
        return self.status in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )


@dataclass(frozen=True)
class ObservedPosition:
    """Position observed from stream or REST snapshot.

    Attributes:
        symbol: Trading symbol
        position_amt: Current position (positive=long, negative=short)
        entry_price: Average entry price
        unrealized_pnl: Current unrealized P&L
        ts_observed: When we observed this
        source: "stream" or "rest"
    """

    symbol: str
    position_amt: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    ts_observed: int
    source: str = "stream"

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "position_amt": str(self.position_amt),
            "entry_price": str(self.entry_price),
            "unrealized_pnl": str(self.unrealized_pnl),
            "ts_observed": self.ts_observed,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObservedPosition:
        """Create from dict."""
        return cls(
            symbol=d["symbol"],
            position_amt=Decimal(d["position_amt"]),
            entry_price=Decimal(d["entry_price"]),
            unrealized_pnl=Decimal(d["unrealized_pnl"]),
            ts_observed=d["ts_observed"],
            source=d.get("source", "stream"),
        )

    def to_json(self) -> str:
        """Convert to deterministic JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Mismatch:
    """Detected reconciliation mismatch.

    Attributes:
        mismatch_type: Type of mismatch
        symbol: Trading symbol
        client_order_id: Order ID (if applicable)
        expected: Expected state (as dict, or None)
        observed: Observed state (as dict, or None)
        ts_detected: When mismatch was detected
        action_plan: Proposed remediation (text only in v0.1)
    """

    mismatch_type: MismatchType
    symbol: str
    client_order_id: str | None
    expected: dict[str, Any] | None
    observed: dict[str, Any] | None
    ts_detected: int
    action_plan: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "mismatch_type": self.mismatch_type.value,
            "symbol": self.symbol,
            "client_order_id": self.client_order_id,
            "expected": self.expected,
            "observed": self.observed,
            "ts_detected": self.ts_detected,
            "action_plan": self.action_plan,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Mismatch:
        """Create from dict."""
        return cls(
            mismatch_type=MismatchType(d["mismatch_type"]),
            symbol=d["symbol"],
            client_order_id=d.get("client_order_id"),
            expected=d.get("expected"),
            observed=d.get("observed"),
            ts_detected=d["ts_detected"],
            action_plan=d["action_plan"],
        )

    def to_json(self) -> str:
        """Convert to deterministic JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_log_extra(self) -> dict[str, Any]:
        """Generate extra dict for structured logging."""
        return {
            "mismatch_type": self.mismatch_type.value,
            "symbol": self.symbol,
            "client_order_id": self.client_order_id,
            "action_plan": self.action_plan,
        }
