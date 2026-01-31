"""Risk gate for position and loss limits.

Implements risk checks based on:
- Max notional exposure (per symbol and total)
- Daily loss limit (PnL tracking)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from grinder.gating.types import GateReason, GatingResult


@dataclass
class RiskGate:
    """Risk gate for position and loss limits.

    Attributes:
        max_notional_per_symbol: Maximum notional exposure per symbol (USD).
        max_notional_total: Maximum total notional exposure (USD).
        daily_loss_limit: Maximum daily loss before blocking (USD, positive value).
    """

    max_notional_per_symbol: Decimal = Decimal("5000")
    max_notional_total: Decimal = Decimal("20000")
    daily_loss_limit: Decimal = Decimal("500")

    # Internal state
    _notional_by_symbol: dict[str, Decimal] = field(default_factory=dict, repr=False)
    _realized_pnl: Decimal = field(default=Decimal("0"), repr=False)
    _unrealized_pnl: Decimal = field(default=Decimal("0"), repr=False)

    def check_order(
        self,
        symbol: str,
        notional: Decimal,
    ) -> GatingResult:
        """Check if an order is allowed given current risk state.

        Args:
            symbol: Trading symbol.
            notional: Notional value of the proposed order (USD).

        Returns:
            GatingResult indicating whether the order is allowed.
        """
        # Check daily loss limit
        total_pnl = self._realized_pnl + self._unrealized_pnl
        if total_pnl < -self.daily_loss_limit:
            return GatingResult.block(
                GateReason.DAILY_LOSS_LIMIT_EXCEEDED,
                {
                    "total_pnl": str(total_pnl),
                    "daily_loss_limit": str(self.daily_loss_limit),
                },
            )

        # Check per-symbol notional
        current_symbol_notional = self._notional_by_symbol.get(symbol, Decimal("0"))
        new_symbol_notional = current_symbol_notional + notional
        if new_symbol_notional > self.max_notional_per_symbol:
            return GatingResult.block(
                GateReason.MAX_NOTIONAL_EXCEEDED,
                {
                    "symbol": symbol,
                    "current_notional": str(current_symbol_notional),
                    "proposed_notional": str(notional),
                    "new_total": str(new_symbol_notional),
                    "limit": str(self.max_notional_per_symbol),
                    "scope": "symbol",
                },
            )

        # Check total notional
        current_total = sum(self._notional_by_symbol.values(), Decimal("0"))
        new_total = current_total + notional
        if new_total > self.max_notional_total:
            return GatingResult.block(
                GateReason.MAX_NOTIONAL_EXCEEDED,
                {
                    "current_total": str(current_total),
                    "proposed_notional": str(notional),
                    "new_total": str(new_total),
                    "limit": str(self.max_notional_total),
                    "scope": "total",
                },
            )

        return GatingResult.allow(
            {
                "symbol": symbol,
                "symbol_notional": str(new_symbol_notional),
                "total_notional": str(new_total),
            }
        )

    def record_order(self, symbol: str, notional: Decimal) -> None:
        """Record that an order was placed.

        Args:
            symbol: Trading symbol.
            notional: Notional value of the order (USD).
        """
        current = self._notional_by_symbol.get(symbol, Decimal("0"))
        self._notional_by_symbol[symbol] = current + notional

    def record_fill(self, symbol: str, notional_delta: Decimal, pnl_delta: Decimal) -> None:
        """Record a fill event.

        Args:
            symbol: Trading symbol.
            notional_delta: Change in notional (negative for position reduction).
            pnl_delta: Realized PnL from this fill.
        """
        current = self._notional_by_symbol.get(symbol, Decimal("0"))
        new_notional = current + notional_delta
        if new_notional <= 0:
            self._notional_by_symbol.pop(symbol, None)
        else:
            self._notional_by_symbol[symbol] = new_notional

        self._realized_pnl += pnl_delta

    def update_unrealized_pnl(self, pnl: Decimal) -> None:
        """Update unrealized PnL (mark-to-market).

        Args:
            pnl: Current unrealized PnL.
        """
        self._unrealized_pnl = pnl

    def reset(self) -> None:
        """Reset all risk state."""
        self._notional_by_symbol.clear()
        self._realized_pnl = Decimal("0")
        self._unrealized_pnl = Decimal("0")

    @property
    def total_notional(self) -> Decimal:
        """Current total notional exposure."""
        return sum(self._notional_by_symbol.values(), Decimal("0"))

    @property
    def total_pnl(self) -> Decimal:
        """Current total PnL (realized + unrealized)."""
        return self._realized_pnl + self._unrealized_pnl

    def get_symbol_notional(self, symbol: str) -> Decimal:
        """Get notional exposure for a symbol."""
        return self._notional_by_symbol.get(symbol, Decimal("0"))
