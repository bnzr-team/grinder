"""Data connector protocol for market data streams.

This module defines the DataConnector protocol for streaming market data
(Snapshots) from various sources. Unlike ExchangeConnector which handles
trading operations, DataConnector focuses purely on data ingestion.

Key design decisions (see ADR-012):
- Async iterator pattern for streaming
- Explicit connect/close lifecycle
- Hardening hooks: timeouts, retry policy, idempotency
- Deterministic behavior for replay/paper testing
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from grinder.contracts import Snapshot


class ConnectorState(Enum):
    """Connector lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


@dataclass(frozen=True)
class RetryConfig:
    """Retry policy configuration.

    Attributes:
        max_retries: Maximum retry attempts (0 = no retries)
        base_delay_ms: Initial delay between retries in milliseconds
        max_delay_ms: Maximum delay cap in milliseconds
        backoff_multiplier: Multiplier for exponential backoff (1.0 = linear)
    """

    max_retries: int = 3
    base_delay_ms: int = 1000
    max_delay_ms: int = 30000
    backoff_multiplier: float = 2.0

    def get_delay_ms(self, attempt: int) -> int:
        """Calculate delay for given attempt number (0-indexed).

        Uses exponential backoff with cap.
        """
        if attempt < 0:
            return self.base_delay_ms

        delay = self.base_delay_ms * (self.backoff_multiplier**attempt)
        return min(int(delay), self.max_delay_ms)


@dataclass(frozen=True)
class TimeoutConfig:
    """Timeout configuration.

    Attributes:
        connect_timeout_ms: Timeout for initial connection
        read_timeout_ms: Timeout for reading next snapshot (0 = no timeout)
    """

    connect_timeout_ms: int = 10000
    read_timeout_ms: int = 5000


class DataConnector(ABC):
    """Abstract base class for market data connectors.

    Provides streaming access to Snapshot data with lifecycle management
    and hardening features (timeouts, retries, idempotency).

    Implementations must ensure:
    - Deterministic ordering of snapshots (by timestamp)
    - Idempotency guards (no duplicate snapshots delivered)
    - Clean resource management (connect/close lifecycle)

    Example usage:
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()
        try:
            async for snapshot in connector.iter_snapshots():
                process(snapshot)
        finally:
            await connector.close()
    """

    @property
    @abstractmethod
    def state(self) -> ConnectorState:
        """Get current connector state."""
        ...

    @property
    @abstractmethod
    def last_seen_ts(self) -> int | None:
        """Get timestamp of last delivered snapshot (for idempotency).

        Returns None if no snapshots delivered yet.
        """
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to data source.

        Raises:
            ConnectionError: If connection fails after retries
            asyncio.TimeoutError: If connection times out
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connection and release resources.

        Safe to call multiple times. Idempotent.
        """
        ...

    @abstractmethod
    def iter_snapshots(self) -> AsyncIterator[Snapshot]:
        """Iterate over snapshots from data source.

        Yields snapshots in timestamp order. Guarantees:
        - No duplicate timestamps (idempotency)
        - Monotonically increasing timestamps
        - Clean termination on end-of-stream

        Raises:
            ConnectionError: If not connected
            asyncio.TimeoutError: If read times out (if timeout configured)
        """
        ...

    @abstractmethod
    async def reconnect(self) -> None:
        """Reconnect after failure.

        Uses retry policy for backoff. Resumes from last_seen_ts.

        Raises:
            ConnectionError: If reconnection fails after max retries
        """
        ...
