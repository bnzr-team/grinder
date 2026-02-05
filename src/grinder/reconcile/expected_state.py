"""Expected state store with ring buffer and TTL.

See ADR-042 for design decisions.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.core import OrderState
from grinder.reconcile.types import ExpectedOrder, ExpectedPosition

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class ExpectedStateStore:
    """Store for expected orders and positions.

    Features:
    - Ring buffer: max_orders limit (default 200)
    - TTL eviction: orders older than ttl_ms are evicted (default 24h)
    - Thread-safe via simple dict operations (GIL)

    Usage:
        store = ExpectedStateStore(max_orders=200, ttl_ms=86400000)
        store.record_order(expected_order)
        store.mark_cancelled("grinder_BTCUSDT_1_123_1")
        active_orders = store.get_active_orders()
    """

    max_orders: int = 200
    ttl_ms: int = 86_400_000  # 24 hours

    # Keyed by client_order_id (OrderedDict preserves insertion order for FIFO eviction)
    _orders: OrderedDict[str, ExpectedOrder] = field(default_factory=OrderedDict)
    _positions: dict[str, ExpectedPosition] = field(default_factory=dict)

    # Clock injection for testing
    _clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))

    def record_order(self, order: ExpectedOrder) -> None:
        """Record a placed order as expected.

        Called when smoke harness successfully sends place_order().
        """
        # Evict old entries if at capacity
        self._evict_if_needed()

        # Add new order
        self._orders[order.client_order_id] = order

    def mark_filled(self, client_order_id: str) -> None:
        """Mark an order as filled (terminal state)."""
        if client_order_id in self._orders:
            old = self._orders[client_order_id]
            # Create new frozen dataclass with updated status
            self._orders[client_order_id] = ExpectedOrder(
                client_order_id=old.client_order_id,
                symbol=old.symbol,
                side=old.side,
                order_type=old.order_type,
                price=old.price,
                orig_qty=old.orig_qty,
                ts_created=old.ts_created,
                expected_status=OrderState.FILLED,
            )

    def mark_cancelled(self, client_order_id: str) -> None:
        """Mark an order as cancelled (terminal state)."""
        if client_order_id in self._orders:
            old = self._orders[client_order_id]
            # Create new frozen dataclass with updated status
            self._orders[client_order_id] = ExpectedOrder(
                client_order_id=old.client_order_id,
                symbol=old.symbol,
                side=old.side,
                order_type=old.order_type,
                price=old.price,
                orig_qty=old.orig_qty,
                ts_created=old.ts_created,
                expected_status=OrderState.CANCELLED,
            )

    def remove_order(self, client_order_id: str) -> None:
        """Remove an order from expected state (for cleanup after terminal)."""
        self._orders.pop(client_order_id, None)

    def get_order(self, client_order_id: str) -> ExpectedOrder | None:
        """Get expected order by client_order_id."""
        return self._orders.get(client_order_id)

    def get_active_orders(self) -> list[ExpectedOrder]:
        """Get all non-terminal expected orders (within TTL)."""
        now = self._clock()
        cutoff = now - self.ttl_ms

        active = []
        for order in self._orders.values():
            # Skip TTL-expired
            if order.ts_created < cutoff:
                continue
            # Skip terminal
            if order.expected_status in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            ):
                continue
            active.append(order)

        return active

    def get_all_orders(self) -> list[ExpectedOrder]:
        """Get all expected orders (including terminal)."""
        return list(self._orders.values())

    def set_position(self, position: ExpectedPosition) -> None:
        """Set expected position for symbol."""
        self._positions[position.symbol] = position

    def get_position(self, symbol: str) -> ExpectedPosition | None:
        """Get expected position for symbol."""
        return self._positions.get(symbol)

    def get_all_positions(self) -> list[ExpectedPosition]:
        """Get all expected positions."""
        return list(self._positions.values())

    def _evict_if_needed(self) -> None:
        """Evict oldest entries if at capacity or TTL expired."""
        now = self._clock()
        cutoff = now - self.ttl_ms

        # First pass: remove TTL-expired terminal orders
        to_remove = []
        for cid, order in self._orders.items():
            if order.ts_created < cutoff and order.expected_status in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            ):
                to_remove.append(cid)

        for cid in to_remove:
            del self._orders[cid]

        # Second pass: enforce max_orders (FIFO eviction of oldest terminal)
        while len(self._orders) >= self.max_orders:
            # Find oldest terminal order to evict
            evicted = False
            for cid, order in self._orders.items():
                if order.expected_status in (
                    OrderState.FILLED,
                    OrderState.CANCELLED,
                    OrderState.REJECTED,
                    OrderState.EXPIRED,
                ):
                    del self._orders[cid]
                    evicted = True
                    break

            if not evicted:
                # No terminal orders - evict oldest regardless
                oldest_cid = next(iter(self._orders))
                del self._orders[oldest_cid]
                break

    def clear(self) -> None:
        """Clear all state."""
        self._orders.clear()
        self._positions.clear()

    @property
    def order_count(self) -> int:
        """Current number of tracked orders."""
        return len(self._orders)
