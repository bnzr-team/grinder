"""Mock Binance WebSocket connector for testing.

This module provides a mock implementation of the DataConnector abstract base class
that reads from fixture files (events.jsonl) and emits Snapshots.

Used for:
- Integration testing without real Binance connection
- Soak testing with controlled data
- Development and debugging
- Timeout and cancellation testing (H1)

See: ADR-012 for connector design decisions
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from grinder.connectors.data_connector import (
    ConnectorState,
    DataConnector,
    RetryConfig,
    TimeoutConfig,
)
from grinder.connectors.errors import (
    ConnectorClosedError,
    ConnectorTimeoutError,
)
from grinder.connectors.timeouts import (
    cancel_tasks_with_timeout,
    wait_for_with_op,
)
from grinder.contracts import Snapshot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class MockConnectorStats:
    """Statistics for monitoring connector behavior."""

    events_loaded: int = 0
    snapshots_delivered: int = 0
    duplicates_skipped: int = 0
    reconnect_attempts: int = 0
    timeouts: int = 0
    tasks_cancelled: int = 0
    tasks_force_killed: int = 0
    errors: list[str] = field(default_factory=list)


class BinanceWsMockConnector(DataConnector):
    """Mock connector that reads from fixture files.

    Simulates a Binance WebSocket stream by reading events.jsonl
    and yielding Snapshots with configurable timing.

    Features:
    - Reads from standard fixture format (events.jsonl)
    - Configurable read delay for simulating real-time
    - Idempotency via timestamp tracking
    - Retry/reconnect logic (for interface compliance)
    - Statistics for testing/debugging

    Example:
        connector = BinanceWsMockConnector(
            fixture_path=Path("tests/fixtures/sample_day"),
            read_delay_ms=0,  # No delay for tests
        )
        await connector.connect()
        async for snapshot in connector.iter_snapshots():
            print(f"Got {snapshot.symbol} @ {snapshot.mid_price}")
        await connector.close()
    """

    def __init__(
        self,
        fixture_path: Path,
        *,
        read_delay_ms: int = 0,
        timeout_config: TimeoutConfig | None = None,
        retry_config: RetryConfig | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        """Initialize mock connector.

        Args:
            fixture_path: Path to fixture directory containing events.jsonl
            read_delay_ms: Delay between yielding snapshots (ms). 0 = no delay.
            timeout_config: Timeout settings (not used in mock, but for interface)
            retry_config: Retry settings (not used in mock, but for interface)
            symbols: Filter to specific symbols (None = all symbols)
        """
        self._fixture_path = fixture_path
        self._read_delay_ms = read_delay_ms
        self._timeout_config = timeout_config or TimeoutConfig()
        self._retry_config = retry_config or RetryConfig()
        self._symbols = set(symbols) if symbols else None

        # Internal state
        self._state = ConnectorState.DISCONNECTED
        self._last_seen_ts: int | None = None
        self._events: list[dict[str, Any]] = []
        self._cursor: int = 0
        self._stats = MockConnectorStats()

        # Task tracking for clean shutdown (no zombies)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_name_prefix = f"mock_connector_{id(self)}_"

    @property
    def state(self) -> ConnectorState:
        """Get current connector state."""
        return self._state

    @property
    def last_seen_ts(self) -> int | None:
        """Get timestamp of last delivered snapshot."""
        return self._last_seen_ts

    @property
    def stats(self) -> MockConnectorStats:
        """Get connector statistics."""
        return self._stats

    async def connect(self) -> None:
        """Load fixture and prepare for streaming.

        Raises:
            ConnectionError: If fixture not found or invalid
            ConnectorTimeoutError: If connection times out
        """
        if self._state == ConnectorState.CONNECTED:
            return  # Already connected

        self._state = ConnectorState.CONNECTING

        try:
            # Use timeout wrapper for connect operation
            await wait_for_with_op(
                self._do_connect(),
                timeout_ms=self._timeout_config.connect_timeout_ms,
                op="connect",
            )
        except ConnectorTimeoutError:
            self._state = ConnectorState.DISCONNECTED
            self._stats.timeouts += 1
            self._stats.errors.append("Connect timeout")
            raise
        except FileNotFoundError as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Fixture not found: {e}")
            raise ConnectionError(f"Fixture not found: {self._fixture_path}") from e
        except json.JSONDecodeError as e:
            self._state = ConnectorState.DISCONNECTED
            self._stats.errors.append(f"Invalid JSON: {e}")
            raise ConnectionError(f"Invalid fixture JSON: {e}") from e

    async def _do_connect(self) -> None:
        """Internal connect implementation."""
        # Simulate minimal async yield
        await asyncio.sleep(0.001)

        self._events = self._load_fixture()
        self._cursor = 0
        self._stats.events_loaded = len(self._events)
        self._state = ConnectorState.CONNECTED

    async def close(self) -> None:
        """Close connector and release resources.

        Cancels all background tasks and waits for completion within
        close_timeout_ms. Safe to call multiple times. Idempotent.
        """
        if self._state == ConnectorState.CLOSED:
            return

        self._state = ConnectorState.CLOSED

        # Cancel and await all tracked tasks
        if self._tasks:
            cancelled, timed_out = await cancel_tasks_with_timeout(
                self._tasks,
                timeout_ms=self._timeout_config.close_timeout_ms,
                task_name_prefix=self._task_name_prefix,
            )
            self._stats.tasks_cancelled += cancelled
            self._stats.tasks_force_killed += timed_out

        self._tasks.clear()
        self._events = []
        self._cursor = 0

    async def iter_snapshots(self) -> AsyncIterator[Snapshot]:
        """Iterate over snapshots from fixture.

        Yields snapshots in timestamp order with idempotency guarantees.

        Raises:
            ConnectionError: If not connected
            ConnectorClosedError: If connector is closed during iteration
            ConnectorTimeoutError: If read times out
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("iterate")
        if self._state != ConnectorState.CONNECTED:
            raise ConnectionError(f"Cannot iterate: connector state is {self._state.value}")

        while self._cursor < len(self._events):
            # Check if closed during iteration (state can change externally)
            if self._state != ConnectorState.CONNECTED:
                raise ConnectorClosedError("iterate")

            event = self._events[self._cursor]
            self._cursor += 1

            # Only process SNAPSHOT events
            if event.get("type") != "SNAPSHOT":
                continue

            # Symbol filter
            symbol = event.get("symbol", "")
            if self._symbols and symbol not in self._symbols:
                continue

            # Idempotency: skip if we've already seen this timestamp
            ts = event.get("ts", 0)
            if self._last_seen_ts is not None and ts <= self._last_seen_ts:
                self._stats.duplicates_skipped += 1
                continue

            # Parse and yield snapshot
            try:
                snapshot = self._parse_snapshot(event)
                self._last_seen_ts = ts
                self._stats.snapshots_delivered += 1

                # Optional delay for simulating real-time (with read timeout)
                if self._read_delay_ms > 0:
                    try:
                        await wait_for_with_op(
                            asyncio.sleep(self._read_delay_ms / 1000),
                            timeout_ms=self._timeout_config.read_timeout_ms,
                            op="read",
                        )
                    except ConnectorTimeoutError:
                        self._stats.timeouts += 1
                        raise

                yield snapshot

            except (KeyError, ValueError) as e:
                self._stats.errors.append(f"Parse error at ts={ts}: {e}")
                continue

    async def reconnect(self) -> None:
        """Reconnect from last seen position.

        For mock connector, this just resets cursor to resume position.

        Raises:
            ConnectorClosedError: If connector is closed
        """
        if self._state == ConnectorState.CLOSED:
            raise ConnectorClosedError("reconnect")

        self._state = ConnectorState.RECONNECTING
        self._stats.reconnect_attempts += 1

        # Find cursor position for resumption
        if self._last_seen_ts is not None:
            for i, event in enumerate(self._events):
                if event.get("ts", 0) > self._last_seen_ts:
                    self._cursor = i
                    break
            else:
                # All events already seen
                self._cursor = len(self._events)
        else:
            self._cursor = 0

        self._state = ConnectorState.CONNECTED

    def _load_fixture(self) -> list[dict[str, Any]]:
        """Load and parse fixture events."""
        events: list[dict[str, Any]] = []

        jsonl_path = self._fixture_path / "events.jsonl"
        json_path = self._fixture_path / "events.json"

        if jsonl_path.exists():
            with jsonl_path.open() as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        events.append(json.loads(stripped))
        elif json_path.exists():
            with json_path.open() as f:
                events = json.load(f)
        else:
            raise FileNotFoundError(f"No events.jsonl or events.json in {self._fixture_path}")

        # Sort by timestamp for determinism
        events.sort(key=lambda e: e.get("ts", 0))
        return events

    def _parse_snapshot(self, event: dict[str, Any]) -> Snapshot:
        """Parse event dict into Snapshot."""
        return Snapshot(
            ts=event["ts"],
            symbol=event["symbol"],
            bid_price=Decimal(event["bid_price"]),
            ask_price=Decimal(event["ask_price"]),
            bid_qty=Decimal(event["bid_qty"]),
            ask_qty=Decimal(event["ask_qty"]),
            last_price=Decimal(event["last_price"]),
            last_qty=Decimal(event["last_qty"]),
        )

    @property
    def active_tasks(self) -> set[asyncio.Task[Any]]:
        """Get set of active tasks (for testing/debugging)."""
        return {t for t in self._tasks if not t.done()}

    @property
    def timeout_config(self) -> TimeoutConfig:
        """Get timeout configuration."""
        return self._timeout_config

    def reset(self) -> None:
        """Reset connector to initial state (for testing).

        Clears last_seen_ts and resets cursor.
        """
        self._last_seen_ts = None
        self._cursor = 0
        self._stats = MockConnectorStats()
        if self._events:
            self._stats.events_loaded = len(self._events)
