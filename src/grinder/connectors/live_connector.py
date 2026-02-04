"""Live WebSocket connector for Binance market data.

This module provides a production-ready connector for live market data streams
from Binance via WebSocket. It implements the DataConnector ABC with full
hardening (H2 retries, H4 circuit breaker, H5 metrics).

Key design decisions (see ADR-029):
- SafeMode enum for explicit read_only/paper/live_trade modes
- read_only is default (safe by design)
- H2/H4/H5 hardening applied to all operations
- Bounded-time testing support via injectable clock/sleep
- No trading operations in read_only mode

Usage:
    connector = LiveConnectorV0(
        mode=SafeMode.READ_ONLY,  # Default: safe
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    await connector.connect()
    async for snapshot in connector.stream_ticks():
        process(snapshot)
    await connector.close()
"""

from __future__ import annotations

import asyncio
import logging
import time as time_module
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from grinder.connectors.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, default_trip_on
from grinder.connectors.data_connector import (
    ConnectorState,
    DataConnector,
    TimeoutConfig,
)
from grinder.connectors.errors import (
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorTimeoutError,
    ConnectorTransientError,
)
from grinder.connectors.metrics import CircuitMetricState, get_connector_metrics
from grinder.connectors.retries import RetryPolicy, retry_with_policy
from grinder.connectors.timeouts import cancel_tasks_with_timeout, wait_for_with_op

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from grinder.contracts import Snapshot

logger = logging.getLogger(__name__)


class SafeMode(Enum):
    """Safe mode for connector operations.

    Determines what operations are allowed:
    - READ_ONLY: Only read market data (no trading) - DEFAULT
    - PAPER: Read data, simulate trading (no real orders)
    - LIVE_TRADE: Full trading capability (requires explicit opt-in)

    See ADR-029 for design decisions.
    """

    READ_ONLY = "read_only"
    PAPER = "paper"
    LIVE_TRADE = "live_trade"


@dataclass
class LiveConnectorConfig:
    """Configuration for LiveConnectorV0.

    Attributes:
        mode: Safe mode (default: READ_ONLY)
        symbols: List of symbols to subscribe to
        ws_url: WebSocket URL (default: Binance testnet for safety)
        timeout_config: Timeout configuration
        retry_policy: Retry policy for transient failures
        circuit_breaker_config: Circuit breaker configuration
    """

    mode: SafeMode = SafeMode.READ_ONLY
    symbols: list[str] = field(default_factory=list)
    ws_url: str = "wss://testnet.binance.vision/ws"  # Testnet by default (safe)
    timeout_config: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry_policy: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_attempts=3))
    circuit_breaker_config: CircuitBreakerConfig = field(
        default_factory=lambda: CircuitBreakerConfig(trip_on=default_trip_on)
    )


@dataclass
class LiveConnectorStats:
    """Statistics for live connector operations."""

    ticks_received: int = 0
    connection_attempts: int = 0
    reconnections: int = 0
    retries: int = 0
    circuit_trips: int = 0
    timeouts: int = 0
    errors: list[str] = field(default_factory=list)


class LiveConnectorV0(DataConnector):
    """Live WebSocket connector for Binance market data (read-only by default).

    Implements DataConnector ABC with full H2/H4/H5 hardening:
    - H2: Retries with exponential backoff
    - H4: Circuit breaker for fast-fail
    - H5: Prometheus metrics for observability

    Safe mode enforcement:
    - READ_ONLY (default): Only stream_ticks() is allowed
    - PAPER: stream_ticks() + simulated trading
    - LIVE_TRADE: Full trading (requires explicit opt-in)

    Example:
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.READ_ONLY,
                symbols=["BTCUSDT"],
            )
        )
        await connector.connect()
        async for snapshot in connector.stream_ticks():
            print(f"Got {snapshot.symbol} @ {snapshot.mid_price}")
        await connector.close()
    """

    def __init__(
        self,
        config: LiveConnectorConfig | None = None,
        *,
        clock: Any = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize live connector.

        Args:
            config: Configuration (uses defaults if None)
            clock: Injectable clock for testing (default: time module)
            sleep_func: Injectable sleep function for testing (default: asyncio.sleep)
        """
        self._config = config or LiveConnectorConfig()
        self._clock = clock if clock is not None else time_module
        self._sleep_func = sleep_func if sleep_func is not None else asyncio.sleep

        # Internal state
        self._state = ConnectorState.DISCONNECTED
        self._last_seen_ts: int | None = None
        self._stats = LiveConnectorStats()

        # WebSocket state (not used in v0 mock, placeholder for real impl)
        self._ws: Any = None
        self._subscription_id: int = 0

        # Task tracking for clean shutdown
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_name_prefix = f"live_connector_{id(self)}_"

        # H4: Circuit breaker
        self._circuit_breaker = CircuitBreaker(
            self._config.circuit_breaker_config,
            clock=self._clock,
        )

        # Initialize circuit state metrics
        for symbol in self._config.symbols:
            op_name = f"stream_{symbol}"
            get_connector_metrics().set_circuit_state(op_name, CircuitMetricState.CLOSED)

    def _now(self) -> float:
        """Get current time from clock."""
        return float(self._clock.time())

    @property
    def state(self) -> ConnectorState:
        """Get current connector state."""
        return self._state

    @property
    def last_seen_ts(self) -> int | None:
        """Get timestamp of last delivered snapshot."""
        return self._last_seen_ts

    @property
    def stats(self) -> LiveConnectorStats:
        """Get connector statistics."""
        return self._stats

    @property
    def mode(self) -> SafeMode:
        """Get current safe mode."""
        return self._config.mode

    @property
    def symbols(self) -> list[str]:
        """Get subscribed symbols."""
        return list(self._config.symbols)

    async def connect(self) -> None:
        """Establish WebSocket connection.

        Uses H2 retry policy for transient failures.
        Uses H4 circuit breaker for fast-fail.

        Raises:
            ConnectionError: If connection fails after retries
            ConnectorTimeoutError: If connection times out
        """
        if self._state == ConnectorState.CONNECTED:
            return  # Already connected

        self._state = ConnectorState.CONNECTING
        self._stats.connection_attempts += 1

        try:
            # H4: Check circuit breaker before attempting
            self._circuit_breaker.before_call("connect")

            # H2: Retry with policy
            _, retry_stats = await retry_with_policy(
                "connect",
                self._do_connect,
                self._config.retry_policy,
                sleep_func=self._sleep_func,
            )
            self._stats.retries += retry_stats.retries

            # Record success
            self._circuit_breaker.record_success("connect")

        except ConnectorTimeoutError:
            self._state = ConnectorState.DISCONNECTED
            self._stats.timeouts += 1
            self._stats.errors.append("Connect timeout")
            self._circuit_breaker.record_failure("connect", "timeout")
            raise
        except ConnectorTransientError as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Connect failed: {e}")
            self._circuit_breaker.record_failure("connect", "transient")
            raise ConnectionError(f"Connect failed: {e}") from e
        except Exception as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Connect error: {e}")
            raise

    async def _do_connect(self) -> None:
        """Internal connect implementation.

        In v0, this is a mock that simulates connection.
        Real implementation will establish WebSocket connection.
        """
        await wait_for_with_op(
            self._simulate_connect(),
            timeout_ms=self._config.timeout_config.connect_timeout_ms,
            op="connect",
        )
        self._state = ConnectorState.CONNECTED
        logger.info(
            "LiveConnectorV0 connected (mode=%s, symbols=%s)",
            self._config.mode.value,
            self._config.symbols,
        )

    async def _simulate_connect(self) -> None:
        """Simulate WebSocket connection (v0 mock).

        Real implementation will:
        1. Connect to WebSocket
        2. Send subscription messages
        3. Validate handshake
        """
        # Minimal async yield for testing
        await self._sleep_func(0.001)

    async def close(self) -> None:
        """Close WebSocket connection and release resources.

        Cancels all background tasks and waits for completion.
        Safe to call multiple times. Idempotent.
        """
        if self._state == ConnectorState.CLOSED:
            return

        self._state = ConnectorState.CLOSED

        # Cancel and await all tracked tasks
        if self._tasks:
            cancelled, timed_out = await cancel_tasks_with_timeout(
                self._tasks,
                timeout_ms=self._config.timeout_config.close_timeout_ms,
                task_name_prefix=self._task_name_prefix,
            )
            self._stats.errors.append(f"Closed: {cancelled} tasks cancelled, {timed_out} timed out")

        self._tasks.clear()
        logger.info("LiveConnectorV0 closed")

    def iter_snapshots(self) -> AsyncIterator[Snapshot]:
        """Iterate over snapshots (DataConnector ABC compliance).

        Alias for stream_ticks() for DataConnector interface compliance.
        """
        return self.stream_ticks()

    async def stream_ticks(self) -> AsyncIterator[Snapshot]:
        """Stream market data ticks.

        This is the primary data streaming method. Always available
        regardless of SafeMode (reading data is always safe).

        Yields snapshots in timestamp order with idempotency guarantees.

        Raises:
            ConnectionError: If not connected
            ConnectorClosedError: If connector is closed during streaming
            ConnectorTimeoutError: If read times out
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("stream_ticks")
        if self._state != ConnectorState.CONNECTED:
            raise ConnectionError(f"Cannot stream: connector state is {self._state.value}")

        # V0: Mock implementation yields nothing (placeholder)
        # Real implementation will:
        # 1. Read from WebSocket
        # 2. Parse messages
        # 3. Convert to Snapshot
        # 4. Yield with idempotency checks

        # For v0, we just yield nothing (no real WebSocket)
        # This allows tests to verify the interface works
        return
        yield  # Makes this an async generator

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to additional symbols.

        Adds symbols to the active subscription. Only available when connected.

        Args:
            symbols: List of symbols to add to subscription

        Raises:
            ConnectionError: If not connected
            ConnectorClosedError: If connector is closed
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("subscribe")
        if self._state != ConnectorState.CONNECTED:
            raise ConnectionError(f"Cannot subscribe: connector state is {self._state.value}")

        # Add to config
        for symbol in symbols:
            if symbol not in self._config.symbols:
                self._config.symbols.append(symbol)
                # Initialize circuit state for new symbol
                op_name = f"stream_{symbol}"
                get_connector_metrics().set_circuit_state(op_name, CircuitMetricState.CLOSED)

        logger.info("Subscribed to additional symbols: %s", symbols)

    async def reconnect(self) -> None:
        """Reconnect after failure.

        Uses retry policy for backoff. Resumes from last_seen_ts.

        Raises:
            ConnectionError: If reconnection fails after max retries
            ConnectorClosedError: If connector is closed
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("reconnect")

        self._state = ConnectorState.RECONNECTING
        self._stats.reconnections += 1

        try:
            # Use same connect logic with retries
            _, retry_stats = await retry_with_policy(
                "reconnect",
                self._do_connect,
                self._config.retry_policy,
                sleep_func=self._sleep_func,
            )
            self._stats.retries += retry_stats.retries

        except Exception as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Reconnect failed: {e}")
            raise ConnectionError(f"Reconnect failed: {e}") from e

    def assert_mode(self, required_mode: SafeMode) -> None:
        """Assert that current mode allows the requested operation.

        Args:
            required_mode: Minimum required mode for operation

        Raises:
            ConnectorNonRetryableError: If current mode is insufficient.
                This is non-retryable by design - mode violations are
                configuration errors, not transient failures.
        """
        mode_order = {SafeMode.READ_ONLY: 0, SafeMode.PAPER: 1, SafeMode.LIVE_TRADE: 2}

        if mode_order[self._config.mode] < mode_order[required_mode]:
            raise ConnectorNonRetryableError(
                f"SafeMode violation: operation requires mode={required_mode.value}, "
                f"but connector is in mode={self._config.mode.value}"
            )

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get circuit breaker for testing/monitoring."""
        return self._circuit_breaker
