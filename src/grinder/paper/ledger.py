"""Position and PnL tracking for paper trading.

This module tracks:
- Positions: per-symbol quantity and average entry price
- Realized PnL: profit/loss from closed positions
- Unrealized PnL: mark-to-market value of open positions

All calculations use Decimal for determinism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.paper.fills import Fill


@dataclass
class PositionState:
    """Current position state for a symbol.

    Attributes:
        quantity: Signed quantity (+long, -short)
        avg_entry_price: Weighted average entry price
        realized_pnl: Cumulative realized PnL for this symbol
    """

    quantity: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_entry_price: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "quantity": str(self.quantity),
            "avg_entry_price": str(self.avg_entry_price),
            "realized_pnl": str(self.realized_pnl),
        }


@dataclass
class PnLSnapshot:
    """Point-in-time PnL snapshot.

    Attributes:
        ts: Timestamp
        symbol: Trading symbol
        realized_pnl: Cumulative realized PnL
        unrealized_pnl: Current unrealized PnL (mark-to-market)
        total_pnl: realized + unrealized
    """

    ts: int
    symbol: str
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "total_pnl": str(self.total_pnl),
        }


class Ledger:
    """Tracks positions and PnL across symbols.

    Thread-safe: No. Designed for single-threaded paper trading.
    Determinism: Yes. All calculations use Decimal.
    """

    def __init__(self) -> None:
        """Initialize empty ledger."""
        self._positions: dict[str, PositionState] = {}
        self._total_realized_pnl: Decimal = Decimal("0")

    def reset(self) -> None:
        """Reset all positions and PnL."""
        self._positions.clear()
        self._total_realized_pnl = Decimal("0")

    def apply_fill(self, fill: Fill) -> None:
        """Update position based on a fill.

        For BUY: increase position (or reduce short)
        For SELL: decrease position (or reduce long)

        When reducing a position, realized PnL is computed.
        """
        if fill.symbol not in self._positions:
            self._positions[fill.symbol] = PositionState()

        pos = self._positions[fill.symbol]

        # Determine signed quantity change
        # BUY = +qty, SELL = -qty
        signed_qty = fill.quantity if fill.side == "BUY" else -fill.quantity

        # Check if this fill reduces or increases position
        old_qty = pos.quantity
        new_qty = old_qty + signed_qty

        # Same direction (increasing position) or flat -> no realized PnL
        if old_qty == Decimal("0"):
            # Opening new position
            pos.quantity = new_qty
            pos.avg_entry_price = fill.price
        elif (old_qty > 0 and signed_qty > 0) or (old_qty < 0 and signed_qty < 0):
            # Increasing position in same direction - update avg price
            total_cost = pos.avg_entry_price * abs(old_qty) + fill.price * abs(signed_qty)
            pos.quantity = new_qty
            if new_qty != Decimal("0"):
                pos.avg_entry_price = total_cost / abs(new_qty)
        else:
            # Reducing or flipping position - realize PnL
            # Quantity being closed is min(|old_qty|, |signed_qty|)
            closed_qty = min(abs(old_qty), abs(signed_qty))

            # Realized PnL formula:
            # long position: (exit - entry) * qty
            # short position: (entry - exit) * qty
            direction = Decimal("1") if old_qty > 0 else Decimal("-1")
            realized = (fill.price - pos.avg_entry_price) * closed_qty * direction

            pos.realized_pnl += realized
            self._total_realized_pnl += realized

            pos.quantity = new_qty

            # If position flipped, set new avg price from the fill
            if (old_qty > 0 and new_qty < 0) or (old_qty < 0 and new_qty > 0):
                # Flipped - new avg price is fill price for the excess quantity
                pos.avg_entry_price = fill.price
            elif new_qty == Decimal("0"):
                # Fully closed
                pos.avg_entry_price = Decimal("0")
            # else: partially closed, keep original avg_entry_price

    def apply_fills(self, fills: list[Fill]) -> None:
        """Apply multiple fills in order."""
        for fill in fills:
            self.apply_fill(fill)

    def get_position(self, symbol: str) -> PositionState:
        """Get current position for a symbol."""
        return self._positions.get(symbol, PositionState())

    def get_unrealized_pnl(self, symbol: str, current_price: Decimal) -> Decimal:
        """Calculate unrealized PnL for a symbol at current price.

        Args:
            symbol: Trading symbol
            current_price: Current market price for mark-to-market

        Returns:
            Unrealized PnL (positive = profit)
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.quantity == Decimal("0"):
            return Decimal("0")

        # Unrealized is (current - entry) times signed qty
        return (current_price - pos.avg_entry_price) * pos.quantity

    def get_pnl_snapshot(self, ts: int, symbol: str, current_price: Decimal) -> PnLSnapshot:
        """Get PnL snapshot for a symbol.

        Args:
            ts: Timestamp for the snapshot
            symbol: Trading symbol
            current_price: Current price for mark-to-market

        Returns:
            PnLSnapshot with realized, unrealized, and total PnL
        """
        pos = self._positions.get(symbol, PositionState())
        unrealized = self.get_unrealized_pnl(symbol, current_price)

        return PnLSnapshot(
            ts=ts,
            symbol=symbol,
            realized_pnl=pos.realized_pnl,
            unrealized_pnl=unrealized,
            total_pnl=pos.realized_pnl + unrealized,
        )

    def get_all_positions(self) -> dict[str, PositionState]:
        """Get all non-zero positions."""
        return {s: p for s, p in self._positions.items() if p.quantity != Decimal("0")}

    def get_total_realized_pnl(self) -> Decimal:
        """Get total realized PnL across all symbols."""
        return self._total_realized_pnl

    def to_dict(self) -> dict[str, Any]:
        """Convert ledger state to JSON-serializable dict."""
        return {
            "positions": {s: p.to_dict() for s, p in self._positions.items()},
            "total_realized_pnl": str(self._total_realized_pnl),
        }
