"""Observed state store updated from stream and REST.

See ADR-042 for design decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from grinder.core import OrderSide, OrderState
from grinder.reconcile.types import ObservedOrder, ObservedPosition

if TYPE_CHECKING:
    from grinder.execution.futures_events import FuturesOrderEvent, FuturesPositionEvent


@dataclass
class ObservedStateStore:
    """Store for observed orders and positions.

    Updated from:
    - LC-09a user-data stream (FuturesOrderEvent, FuturesPositionEvent)
    - REST snapshots (GET /openOrders, GET /positionRisk)

    Orders keyed by client_order_id.
    Positions keyed by symbol.
    """

    _orders: dict[str, ObservedOrder] = field(default_factory=dict)
    _positions: dict[str, ObservedPosition] = field(default_factory=dict)
    _last_snapshot_ts: int = 0

    def update_from_order_event(self, event: FuturesOrderEvent) -> None:
        """Update observed state from stream ORDER_TRADE_UPDATE."""
        observed = ObservedOrder(
            client_order_id=event.client_order_id,
            symbol=event.symbol,
            order_id=event.order_id,
            side=event.side,
            status=event.status,
            price=event.price,
            orig_qty=event.qty,
            executed_qty=event.executed_qty,
            avg_price=event.avg_price,
            ts_observed=event.ts,
            source="stream",
        )
        self._orders[event.client_order_id] = observed

    def update_from_position_event(self, event: FuturesPositionEvent) -> None:
        """Update observed state from stream ACCOUNT_UPDATE."""
        observed = ObservedPosition(
            symbol=event.symbol,
            position_amt=event.position_amt,
            entry_price=event.entry_price,
            unrealized_pnl=event.unrealized_pnl,
            ts_observed=event.ts,
            source="stream",
        )
        self._positions[event.symbol] = observed

    def update_from_rest_orders(
        self,
        orders: list[dict[str, Any]],
        ts: int,
        symbol_filter: str | None = None,
    ) -> None:
        """Update from REST GET /openOrders response.

        REST provides authoritative snapshot of OPEN orders only.
        Terminal orders are NOT returned by /openOrders.

        Args:
            orders: List of order dicts from Binance response
            ts: Snapshot timestamp
            symbol_filter: Optional symbol to filter
        """
        self._last_snapshot_ts = ts

        for order_data in orders:
            symbol = str(order_data.get("symbol", ""))
            if symbol_filter and symbol != symbol_filter:
                continue

            cid = str(order_data.get("clientOrderId", ""))
            if not cid:
                continue

            side = OrderSide.BUY if order_data.get("side") == "BUY" else OrderSide.SELL
            status = OrderState.OPEN
            if order_data.get("status") == "PARTIALLY_FILLED":
                status = OrderState.PARTIALLY_FILLED

            observed = ObservedOrder(
                client_order_id=cid,
                symbol=symbol,
                order_id=int(order_data.get("orderId", 0)),
                side=side,
                status=status,
                price=Decimal(str(order_data.get("price", "0"))),
                orig_qty=Decimal(str(order_data.get("origQty", "0"))),
                executed_qty=Decimal(str(order_data.get("executedQty", "0"))),
                avg_price=Decimal(str(order_data.get("avgPrice", "0"))),
                ts_observed=ts,
                source="rest",
            )
            self._orders[cid] = observed

        # Note: We do NOT remove orders not in REST snapshot here,
        # because REST only shows OPEN orders. Terminal orders are tracked
        # via stream updates.

    def update_from_rest_positions(
        self,
        positions: list[dict[str, Any]],
        ts: int,
        symbol_filter: str | None = None,
    ) -> None:
        """Update from REST GET /positionRisk response.

        Args:
            positions: List of position dicts from Binance response
            ts: Snapshot timestamp
            symbol_filter: Optional symbol to filter
        """
        self._last_snapshot_ts = ts

        for pos_data in positions:
            symbol = str(pos_data.get("symbol", ""))
            if symbol_filter and symbol != symbol_filter:
                continue

            observed = ObservedPosition(
                symbol=symbol,
                position_amt=Decimal(str(pos_data.get("positionAmt", "0"))),
                entry_price=Decimal(str(pos_data.get("entryPrice", "0"))),
                unrealized_pnl=Decimal(str(pos_data.get("unRealizedProfit", "0"))),
                ts_observed=ts,
                source="rest",
            )
            self._positions[symbol] = observed

    def get_order(self, client_order_id: str) -> ObservedOrder | None:
        """Get observed order by client_order_id."""
        return self._orders.get(client_order_id)

    def get_all_orders(self) -> list[ObservedOrder]:
        """Get all observed orders."""
        return list(self._orders.values())

    def get_open_orders(self) -> list[ObservedOrder]:
        """Get observed orders that are not terminal."""
        return [o for o in self._orders.values() if not o.is_terminal()]

    def get_position(self, symbol: str) -> ObservedPosition | None:
        """Get observed position for symbol."""
        return self._positions.get(symbol)

    def get_all_positions(self) -> list[ObservedPosition]:
        """Get all observed positions."""
        return list(self._positions.values())

    @property
    def last_snapshot_ts(self) -> int:
        """Timestamp of last REST snapshot."""
        return self._last_snapshot_ts

    def clear(self) -> None:
        """Clear all state."""
        self._orders.clear()
        self._positions.clear()
        self._last_snapshot_ts = 0
