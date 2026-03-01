"""Narrow protocol for emergency exit operations.

Separates emergency exit capabilities from the full ExchangePort protocol.
BinanceFuturesPort satisfies this protocol; NoOpExchangePort provides stubs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from decimal import Decimal

    from grinder.core import OrderSide


class EmergencyExitPort(Protocol):
    """Minimal port interface required by EmergencyExitExecutor.

    Only methods needed for the 10.6 emergency exit sequence:
    1. cancel_all_orders - cancel all open orders for a symbol
    2. place_market_order - place MARKET reduce_only to close position
    3. get_positions - read current positions to verify closure
    """

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol.

        Returns:
            Number of orders cancelled (or 1 on success, 0 on failure).
        """
        ...

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        reduce_only: bool = False,
    ) -> str:
        """Place a market order.

        Args:
            symbol: Trading symbol.
            side: BUY or SELL.
            quantity: Order quantity.
            reduce_only: If True, only reduce position (MUST be True for emergency exit).

        Returns:
            order_id string.
        """
        ...

    def get_positions(self, symbol: str) -> list[Any]:
        """Get open positions for a symbol.

        Returns non-zero positions only. Each item must have a `position_amt`
        attribute (Decimal, positive=long, negative=short).
        """
        ...
