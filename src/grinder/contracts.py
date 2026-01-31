"""Domain contracts for GRINDER M1 vertical slice.

These contracts are the SSOT for data flowing through the pipeline:
  Snapshot → PolicyContext → Decision → OrderIntent

All contracts are:
- Immutable (frozen dataclasses)
- JSON-serializable (for fixtures/replay)
- Hashable (for determinism verification)

See: docs/DECISIONS.md ADR-003
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from grinder.core import GridMode, OrderSide


class DecisionReason(Enum):
    """Reason codes for decisions."""

    # Prefilter reasons
    PREFILTER_ALLOW = "PREFILTER_ALLOW"
    PREFILTER_BLOCK_VOLUME = "PREFILTER_BLOCK_VOLUME"
    PREFILTER_BLOCK_SPREAD = "PREFILTER_BLOCK_SPREAD"
    PREFILTER_BLOCK_TOXICITY = "PREFILTER_BLOCK_TOXICITY"

    # Policy reasons
    POLICY_GRID_NORMAL = "POLICY_GRID_NORMAL"
    POLICY_GRID_THROTTLE = "POLICY_GRID_THROTTLE"
    POLICY_PAUSE = "POLICY_PAUSE"
    POLICY_EMERGENCY = "POLICY_EMERGENCY"

    # Risk reasons
    RISK_OK = "RISK_OK"
    RISK_POSITION_LIMIT = "RISK_POSITION_LIMIT"
    RISK_DRAWDOWN_LIMIT = "RISK_DRAWDOWN_LIMIT"
    RISK_KILL_SWITCH = "RISK_KILL_SWITCH"


@dataclass(frozen=True)
class Snapshot:
    """Market data snapshot (L1 tick).

    Minimal representation for M1 vertical slice.
    """

    ts: int  # Unix timestamp milliseconds
    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    last_price: Decimal
    last_qty: Decimal

    @property
    def mid_price(self) -> Decimal:
        """Calculate mid price."""
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread_bps(self) -> float:
        """Calculate spread in basis points."""
        if self.mid_price == 0:
            return 0.0
        return float((self.ask_price - self.bid_price) / self.mid_price * 10000)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "bid_price": str(self.bid_price),
            "ask_price": str(self.ask_price),
            "bid_qty": str(self.bid_qty),
            "ask_qty": str(self.ask_qty),
            "last_price": str(self.last_price),
            "last_qty": str(self.last_qty),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Snapshot:
        """Create from dict."""
        return cls(
            ts=d["ts"],
            symbol=d["symbol"],
            bid_price=Decimal(d["bid_price"]),
            ask_price=Decimal(d["ask_price"]),
            bid_qty=Decimal(d["bid_qty"]),
            ask_qty=Decimal(d["ask_qty"]),
            last_price=Decimal(d["last_price"]),
            last_qty=Decimal(d["last_qty"]),
        )


@dataclass(frozen=True)
class Position:
    """Current position for a symbol."""

    symbol: str
    size: Decimal  # Positive = long, negative = short
    entry_price: Decimal
    unrealized_pnl: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "size": str(self.size),
            "entry_price": str(self.entry_price),
            "unrealized_pnl": str(self.unrealized_pnl),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        """Create from dict."""
        return cls(
            symbol=d["symbol"],
            size=Decimal(d["size"]),
            entry_price=Decimal(d["entry_price"]),
            unrealized_pnl=Decimal(d["unrealized_pnl"]),
        )


@dataclass(frozen=True)
class PolicyContext:
    """Context passed to policy for evaluation.

    Contains everything a policy needs to make a decision.
    """

    snapshot: Snapshot
    position: Position | None
    features: dict[str, float] = field(default_factory=dict)
    # Risk metrics
    daily_pnl: Decimal = Decimal("0")
    max_position_size: Decimal = Decimal("1000")

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "snapshot": self.snapshot.to_dict(),
            "position": self.position.to_dict() if self.position else None,
            "features": self.features,
            "daily_pnl": str(self.daily_pnl),
            "max_position_size": str(self.max_position_size),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PolicyContext:
        """Create from dict."""
        return cls(
            snapshot=Snapshot.from_dict(d["snapshot"]),
            position=Position.from_dict(d["position"]) if d["position"] else None,
            features=d.get("features", {}),
            daily_pnl=Decimal(d.get("daily_pnl", "0")),
            max_position_size=Decimal(d.get("max_position_size", "1000")),
        )


@dataclass(frozen=True)
class OrderIntent:
    """Intent to place an order.

    High-level representation before exchange-specific conversion.
    """

    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    reason: DecisionReason
    level_id: int = 0  # Grid level index

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "reason": self.reason.value,
            "level_id": self.level_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OrderIntent:
        """Create from dict."""
        return cls(
            symbol=d["symbol"],
            side=OrderSide(d["side"]),
            price=Decimal(d["price"]),
            quantity=Decimal(d["quantity"]),
            reason=DecisionReason(d["reason"]),
            level_id=d.get("level_id", 0),
        )


@dataclass(frozen=True)
class Decision:
    """Unified decision output from evaluation cycle.

    Contains all intents and reason codes for audit/replay.
    """

    ts: int  # Timestamp when decision was made
    symbol: str
    mode: GridMode
    reason: DecisionReason
    order_intents: tuple[OrderIntent, ...] = ()
    cancel_order_ids: tuple[str, ...] = ()
    # Diagnostics
    policy_name: str = "UNKNOWN"
    context_hash: str = ""  # For determinism verification

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "mode": self.mode.value,
            "reason": self.reason.value,
            "order_intents": [i.to_dict() for i in self.order_intents],
            "cancel_order_ids": list(self.cancel_order_ids),
            "policy_name": self.policy_name,
            "context_hash": self.context_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decision:
        """Create from dict."""
        return cls(
            ts=d["ts"],
            symbol=d["symbol"],
            mode=GridMode(d["mode"]),
            reason=DecisionReason(d["reason"]),
            order_intents=tuple(OrderIntent.from_dict(i) for i in d.get("order_intents", [])),
            cancel_order_ids=tuple(d.get("cancel_order_ids", [])),
            policy_name=d.get("policy_name", "UNKNOWN"),
            context_hash=d.get("context_hash", ""),
        )

    def to_json(self) -> str:
        """Serialize to JSON string (deterministic)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> Decision:
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(s))
