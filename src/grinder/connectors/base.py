"""Base exchange connector interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from grinder.core import OrderSide, OrderType


class ExchangeConnector(ABC):
    """Abstract base class for exchange connectors."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to exchange."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected."""
        ...

    @abstractmethod
    async def subscribe_trades(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to trade stream."""
        ...

    @abstractmethod
    async def subscribe_book_ticker(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to best bid/ask stream."""
        ...

    @abstractmethod
    async def subscribe_depth(self, symbol: str, levels: int = 5) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to order book depth stream."""
        ...

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Decimal | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Place an order."""
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        """Cancel an order."""
        ...

    @abstractmethod
    async def get_positions(self) -> dict[str, dict[str, Any]]:
        """Get current positions."""
        ...

    @abstractmethod
    async def get_balance(self) -> dict[str, Any]:
        """Get account balance."""
        ...
