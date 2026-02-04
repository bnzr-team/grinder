"""Idempotent wrapper for ExchangePort.

Provides idempotency guarantees for write operations by wrapping
an ExchangePort with an IdempotencyStore.

Key behaviors:
- Same request with same key returns cached result (DONE)
- Concurrent duplicate fails fast with IdempotencyConflictError (INFLIGHT)
- Expired keys allow re-execution
- Side-effects only happen once per unique key

Usage:
    store = InMemoryIdempotencyStore()
    inner_port = NoOpExchangePort()
    port = IdempotentExchangePort(inner_port, store)

    # First call executes
    order_id = port.place_order(symbol="BTC", ...)

    # Second call with same params returns cached order_id
    order_id_2 = port.place_order(symbol="BTC", ...)
    assert order_id == order_id_2

See: ADR-026 for design decisions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.connectors.errors import IdempotencyConflictError
from grinder.connectors.idempotency import (
    IdempotencyEntry,
    IdempotencyStatus,
    InMemoryIdempotencyStore,
    compute_idempotency_key,
    compute_request_fingerprint,
)

if TYPE_CHECKING:
    from decimal import Decimal

    from grinder.connectors.idempotency import IdempotencyStore
    from grinder.core import OrderSide
    from grinder.execution.port import ExchangePort
    from grinder.execution.types import OrderRecord


@dataclass
class IdempotentPortStats:
    """Statistics for idempotent port operations.

    Attributes:
        place_calls: Total place_order calls
        place_cached: Calls that returned cached result
        place_executed: Calls that executed (side-effect)
        place_conflicts: Calls that hit INFLIGHT conflict
        cancel_calls: Total cancel_order calls
        cancel_cached: Calls that returned cached result
        cancel_executed: Calls that executed
        replace_calls: Total replace_order calls
    """

    place_calls: int = 0
    place_cached: int = 0
    place_executed: int = 0
    place_conflicts: int = 0
    cancel_calls: int = 0
    cancel_cached: int = 0
    cancel_executed: int = 0
    replace_calls: int = 0
    replace_cached: int = 0
    replace_executed: int = 0


@dataclass
class IdempotentExchangePort:
    """Wrapper that adds idempotency to ExchangePort operations.

    Thread-safe via IdempotencyStore locking.

    Attributes:
        inner: The underlying ExchangePort
        store: IdempotencyStore for tracking operations
        scope: Scope prefix for idempotency keys (default: "exec")
        inflight_ttl_s: TTL for INFLIGHT entries (default: 300s)
        done_ttl_s: TTL for DONE entries (default: 86400s)
    """

    inner: ExchangePort
    store: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    scope: str = "exec"
    inflight_ttl_s: float = 300.0
    done_ttl_s: float = 86400.0
    _stats: IdempotentPortStats = field(default_factory=IdempotentPortStats)

    @property
    def stats(self) -> IdempotentPortStats:
        """Get current statistics."""
        return self._stats

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
        level_id: int,
        ts: int,
    ) -> str:
        """Place an order with idempotency guarantee.

        If an order with the same parameters was already placed,
        returns the cached order_id instead of placing a new order.

        Raises:
            IdempotencyConflictError: If same request is already INFLIGHT
        """
        self._stats.place_calls += 1

        # Compute idempotency key (ts excluded - same intent = same key regardless of time)
        key = compute_idempotency_key(
            self.scope,
            "place",
            symbol=symbol,
            side=side.value,
            price=price,
            quantity=quantity,
            level_id=level_id,
        )

        fingerprint = compute_request_fingerprint(
            symbol=symbol,
            side=side.value,
            price=price,
            quantity=quantity,
            level_id=level_id,
            ts=ts,
        )

        # Check for existing entry
        existing = self.store.get(key)
        if existing is not None:
            if existing.status == IdempotencyStatus.DONE:
                # Return cached result
                self._stats.place_cached += 1
                return str(existing.result)
            if existing.status == IdempotencyStatus.INFLIGHT:
                # Fast-fail on duplicate in-flight
                self._stats.place_conflicts += 1
                raise IdempotencyConflictError(key, "INFLIGHT")
            # FAILED status - allow retry

        # Create INFLIGHT entry
        entry = IdempotencyEntry(
            key=key,
            status=IdempotencyStatus.INFLIGHT,
            op_name="place",
            request_fingerprint=fingerprint,
            created_at=0,  # Will be set by store
            expires_at=0,  # Will be set by store
        )

        if not self.store.put_if_absent(key, entry, self.inflight_ttl_s):
            # Race condition - another request got there first
            existing = self.store.get(key)
            if existing and existing.status == IdempotencyStatus.DONE:
                self._stats.place_cached += 1
                return str(existing.result)
            self._stats.place_conflicts += 1
            raise IdempotencyConflictError(key, "INFLIGHT")

        # Execute the actual operation
        try:
            order_id = self.inner.place_order(
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity,
                level_id=level_id,
                ts=ts,
            )
            self.store.mark_done(key, order_id)
            self._stats.place_executed += 1
            return order_id
        except Exception as e:
            # Mark as failed so retries can try again
            self.store.mark_failed(key, type(e).__name__)
            raise

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order with idempotency guarantee.

        If the same cancel was already processed, returns cached result.

        Raises:
            IdempotencyConflictError: If same request is already INFLIGHT
        """
        self._stats.cancel_calls += 1

        # For cancel, the order_id IS the idempotency key component
        key = compute_idempotency_key(
            self.scope,
            "cancel",
            symbol="",  # Not needed for cancel
            side="",  # Not needed for cancel
            order_id=order_id,
        )

        fingerprint = compute_request_fingerprint(order_id=order_id)

        # Check for existing entry
        existing = self.store.get(key)
        if existing is not None:
            if existing.status == IdempotencyStatus.DONE:
                self._stats.cancel_cached += 1
                return bool(existing.result)
            if existing.status == IdempotencyStatus.INFLIGHT:
                self._stats.place_conflicts += 1
                raise IdempotencyConflictError(key, "INFLIGHT")

        # Create INFLIGHT entry
        entry = IdempotencyEntry(
            key=key,
            status=IdempotencyStatus.INFLIGHT,
            op_name="cancel",
            request_fingerprint=fingerprint,
            created_at=0,
            expires_at=0,
        )

        if not self.store.put_if_absent(key, entry, self.inflight_ttl_s):
            existing = self.store.get(key)
            if existing and existing.status == IdempotencyStatus.DONE:
                self._stats.cancel_cached += 1
                return bool(existing.result)
            raise IdempotencyConflictError(key, "INFLIGHT")

        # Execute the actual operation
        try:
            result = self.inner.cancel_order(order_id)
            self.store.mark_done(key, result)
            self._stats.cancel_executed += 1
            return result
        except Exception as e:
            self.store.mark_failed(key, type(e).__name__)
            raise

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        ts: int,
    ) -> str:
        """Replace an order with idempotency guarantee.

        Raises:
            IdempotencyConflictError: If same request is already INFLIGHT
        """
        self._stats.replace_calls += 1

        # ts excluded from key - same replace intent = same key regardless of time
        key = compute_idempotency_key(
            self.scope,
            "replace",
            symbol="",  # Order ID implies symbol
            side="",  # Order ID implies side
            order_id=order_id,
            price=new_price,
            quantity=new_quantity,
        )

        fingerprint = compute_request_fingerprint(
            order_id=order_id,
            new_price=new_price,
            new_quantity=new_quantity,
            ts=ts,
        )

        # Check for existing entry
        existing = self.store.get(key)
        if existing is not None:
            if existing.status == IdempotencyStatus.DONE:
                self._stats.replace_cached += 1
                return str(existing.result)
            if existing.status == IdempotencyStatus.INFLIGHT:
                raise IdempotencyConflictError(key, "INFLIGHT")

        # Create INFLIGHT entry
        entry = IdempotencyEntry(
            key=key,
            status=IdempotencyStatus.INFLIGHT,
            op_name="replace",
            request_fingerprint=fingerprint,
            created_at=0,
            expires_at=0,
        )

        if not self.store.put_if_absent(key, entry, self.inflight_ttl_s):
            existing = self.store.get(key)
            if existing and existing.status == IdempotencyStatus.DONE:
                self._stats.replace_cached += 1
                return str(existing.result)
            raise IdempotencyConflictError(key, "INFLIGHT")

        # Execute the actual operation
        try:
            new_order_id = self.inner.replace_order(
                order_id=order_id,
                new_price=new_price,
                new_quantity=new_quantity,
                ts=ts,
            )
            self.store.mark_done(key, new_order_id)
            self._stats.replace_executed += 1
            return new_order_id
        except Exception as e:
            self.store.mark_failed(key, type(e).__name__)
            raise

    def fetch_open_orders(self, symbol: str) -> list[OrderRecord]:
        """Fetch open orders (passthrough, no idempotency needed for reads)."""
        return self.inner.fetch_open_orders(symbol)

    def reset(self) -> None:
        """Reset inner port and stats (for testing)."""
        if hasattr(self.inner, "reset"):
            self.inner.reset()
        if hasattr(self.store, "reset"):
            self.store.reset()
        self._stats = IdempotentPortStats()
