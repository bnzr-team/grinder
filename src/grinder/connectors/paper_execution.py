"""Paper execution adapter for simulated trading.

This module provides a paper trading adapter that simulates order execution
without making real exchange requests. Used when LiveConnectorV0 is in PAPER mode.

Key design decisions (see ADR-030):
- Deterministic order_id generation (seq + clock injection)
- In-memory order state (no persistence)
- Instant fill semantics (v0: all orders fill immediately)
- No network calls (pure simulation)

Usage:
    adapter = PaperExecutionAdapter(clock=fake_clock)
    result = adapter.place_order(OrderRequest(...))
    print(result.order_id)  # Deterministic, predictable
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - used at runtime in dataclasses
from enum import Enum
from typing import Any

from grinder.core import OrderSide, OrderState

logger = logging.getLogger(__name__)


class PaperOrderError(Exception):
    """Error during paper order execution.

    This is a non-retryable error - paper execution failures
    are logical errors, not transient network issues.
    """

    pass


class OrderType(Enum):
    """Order type for paper execution."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True)
class OrderRequest:
    """Request to place or replace an order.

    Attributes:
        symbol: Trading pair symbol (e.g., "BTCUSDT")
        side: Order side (BUY or SELL)
        price: Limit price (required for LIMIT orders)
        quantity: Order quantity
        order_type: Order type (default: LIMIT)
        client_order_id: Optional client-provided order ID
    """

    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    order_type: OrderType = OrderType.LIMIT
    client_order_id: str | None = None


@dataclass
class PaperOrder:
    """Internal order record for paper execution.

    Mutable - state changes as order progresses through lifecycle.
    """

    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    state: OrderState
    order_type: OrderType
    created_ts: int
    updated_ts: int
    client_order_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled_quantity),
            "state": self.state.value,
            "order_type": self.order_type.value,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True)
class OrderResult:
    """Result of an order operation.

    Immutable snapshot of order state after operation.
    """

    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    state: OrderState
    created_ts: int
    updated_ts: int

    @classmethod
    def from_paper_order(cls, order: PaperOrder) -> OrderResult:
        """Create result from paper order."""
        return cls(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            state=order.state,
            created_ts=order.created_ts,
            updated_ts=order.updated_ts,
        )


@dataclass
class PaperExecutionStats:
    """Statistics for paper execution."""

    orders_placed: int = 0
    orders_cancelled: int = 0
    orders_replaced: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0


class PaperExecutionAdapter:
    """Paper execution adapter for simulated trading.

    Provides order execution simulation without real exchange requests.
    All order IDs are deterministic based on sequence counter and clock.

    V0 semantics (ADR-030):
    - place_order: Creates order, instantly fills (no market simulation)
    - cancel_order: Cancels if OPEN/PENDING, error if already FILLED
    - replace_order: Cancel + new place (atomic in paper context)

    Thread safety: NOT thread-safe. Use one adapter per connector instance.
    """

    def __init__(
        self,
        *,
        clock: Any = None,
        order_id_prefix: str = "PAPER",
    ) -> None:
        """Initialize paper execution adapter.

        Args:
            clock: Injectable clock for deterministic timestamps.
                   Must have .time() method returning float seconds.
                   Defaults to None (uses sequence-only IDs).
            order_id_prefix: Prefix for generated order IDs.
        """
        self._clock = clock
        self._order_id_prefix = order_id_prefix

        # Order storage: order_id -> PaperOrder
        self._orders: dict[str, PaperOrder] = {}

        # Sequence counter for deterministic order IDs
        self._seq: int = 0

        # Statistics
        self._stats = PaperExecutionStats()

    def _now_ms(self) -> int:
        """Get current timestamp in milliseconds."""
        if self._clock is not None:
            return int(self._clock.time() * 1000)
        # Fallback: use sequence as pseudo-timestamp for determinism
        return self._seq

    def _generate_order_id(self) -> str:
        """Generate deterministic order ID.

        Format: {prefix}_{seq:08d}
        Example: PAPER_00000001, PAPER_00000002, ...

        Deterministic because seq is monotonically increasing
        and not dependent on wall clock time.
        """
        self._seq += 1
        return f"{self._order_id_prefix}_{self._seq:08d}"

    @property
    def stats(self) -> PaperExecutionStats:
        """Get execution statistics."""
        return self._stats

    @property
    def orders(self) -> dict[str, PaperOrder]:
        """Get all orders (for testing/inspection)."""
        return self._orders.copy()

    def get_order(self, order_id: str) -> PaperOrder | None:
        """Get order by ID."""
        return self._orders.get(order_id)

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Place a new order.

        V0 semantics: Order is created and instantly filled.
        No market simulation, no partial fills.

        Args:
            request: Order request details

        Returns:
            OrderResult with the created (and filled) order

        Raises:
            PaperOrderError: If request is invalid
        """
        # Validate request
        if request.quantity <= 0:
            raise PaperOrderError(f"Invalid quantity: {request.quantity}")
        if request.order_type == OrderType.LIMIT and request.price <= 0:
            raise PaperOrderError(f"Invalid price for LIMIT order: {request.price}")

        # Generate deterministic order ID
        order_id = self._generate_order_id()
        now = self._now_ms()

        # Create order - V0: instant fill
        order = PaperOrder(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            price=request.price,
            quantity=request.quantity,
            filled_quantity=request.quantity,  # V0: instant full fill
            state=OrderState.FILLED,  # V0: instant fill
            order_type=request.order_type,
            created_ts=now,
            updated_ts=now,
            client_order_id=request.client_order_id,
        )

        # Store order
        self._orders[order_id] = order
        self._stats.orders_placed += 1
        self._stats.orders_filled += 1

        logger.debug(
            "Paper order placed and filled: %s %s %s @ %s qty=%s",
            order_id,
            request.side.value,
            request.symbol,
            request.price,
            request.quantity,
        )

        return OrderResult.from_paper_order(order)

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an existing order.

        V0 semantics:
        - If FILLED: raises PaperOrderError (can't cancel filled order)
        - If CANCELLED: returns current state (idempotent)
        - If OPEN/PENDING: cancels and returns updated state

        Args:
            order_id: ID of order to cancel

        Returns:
            OrderResult with the cancelled order state

        Raises:
            PaperOrderError: If order not found or already filled
        """
        order = self._orders.get(order_id)
        if order is None:
            raise PaperOrderError(f"Order not found: {order_id}")

        # Check if already cancelled (idempotent)
        if order.state == OrderState.CANCELLED:
            logger.debug("Order already cancelled: %s", order_id)
            return OrderResult.from_paper_order(order)

        # Can't cancel filled orders
        if order.state == OrderState.FILLED:
            raise PaperOrderError(f"Cannot cancel filled order: {order_id}")

        # Cancel the order
        order.state = OrderState.CANCELLED
        order.updated_ts = self._now_ms()
        self._stats.orders_cancelled += 1

        logger.debug("Paper order cancelled: %s", order_id)

        return OrderResult.from_paper_order(order)

    def replace_order(
        self,
        order_id: str,
        new_price: Decimal | None = None,
        new_quantity: Decimal | None = None,
    ) -> OrderResult:
        """Replace an existing order with new parameters.

        V0 semantics: Cancel old order + place new order (atomic).
        The new order gets a new order_id (cancel+new pattern).

        Args:
            order_id: ID of order to replace
            new_price: New price (uses old price if None)
            new_quantity: New quantity (uses old quantity if None)

        Returns:
            OrderResult with the NEW order (not the cancelled one)

        Raises:
            PaperOrderError: If order not found or cannot be replaced
        """
        order = self._orders.get(order_id)
        if order is None:
            raise PaperOrderError(f"Order not found: {order_id}")

        # Can't replace filled/cancelled orders
        if order.state in (OrderState.FILLED, OrderState.CANCELLED):
            raise PaperOrderError(f"Cannot replace order in state {order.state.value}: {order_id}")

        # Cancel old order
        order.state = OrderState.CANCELLED
        order.updated_ts = self._now_ms()

        # Place new order with updated parameters
        new_request = OrderRequest(
            symbol=order.symbol,
            side=order.side,
            price=new_price if new_price is not None else order.price,
            quantity=new_quantity if new_quantity is not None else order.quantity,
            order_type=order.order_type,
            client_order_id=order.client_order_id,
        )

        self._stats.orders_replaced += 1

        # Place creates the new order
        result = self.place_order(new_request)

        logger.debug(
            "Paper order replaced: %s -> %s",
            order_id,
            result.order_id,
        )

        return result

    def reset(self) -> None:
        """Reset adapter state.

        Clears all orders and resets sequence counter.
        Useful for testing.
        """
        self._orders.clear()
        self._seq = 0
        self._stats = PaperExecutionStats()
        logger.debug("Paper execution adapter reset")
