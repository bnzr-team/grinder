"""Unit tests for DataConnector and BinanceWsMockConnector.

Tests cover:
- Connector lifecycle (connect/close)
- Snapshot iteration
- Idempotency (no duplicate timestamps)
- Symbol filtering
- Retry config calculations
- Error handling
- Statistics tracking
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from grinder.connectors import (
    BinanceWsMockConnector,
    ConnectorState,
    RetryConfig,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from grinder.contracts import Snapshot

# --- Fixtures ---


@pytest.fixture
def sample_events() -> list[dict[str, Any]]:
    """Sample SNAPSHOT events for testing."""
    return [
        {
            "type": "SNAPSHOT",
            "ts": 1000,
            "symbol": "BTCUSDT",
            "bid_price": "50000.00",
            "ask_price": "50001.00",
            "bid_qty": "1.0",
            "ask_qty": "1.0",
            "last_price": "50000.50",
            "last_qty": "0.5",
        },
        {
            "type": "SNAPSHOT",
            "ts": 2000,
            "symbol": "ETHUSDT",
            "bid_price": "3000.00",
            "ask_price": "3001.00",
            "bid_qty": "10.0",
            "ask_qty": "10.0",
            "last_price": "3000.50",
            "last_qty": "5.0",
        },
        {
            "type": "SNAPSHOT",
            "ts": 3000,
            "symbol": "BTCUSDT",
            "bid_price": "50100.00",
            "ask_price": "50101.00",
            "bid_qty": "1.5",
            "ask_qty": "1.5",
            "last_price": "50100.50",
            "last_qty": "0.3",
        },
    ]


@pytest.fixture
def fixture_path(sample_events: list[dict[str, Any]]) -> Iterator[Path]:
    """Create a temporary fixture directory with events.jsonl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        jsonl_path = path / "events.jsonl"
        with jsonl_path.open("w") as f:
            for event in sample_events:
                f.write(json.dumps(event) + "\n")
        yield path


@pytest.fixture
def fixture_path_with_duplicates() -> Iterator[Path]:
    """Create fixture with duplicate timestamps for idempotency testing."""
    events = [
        {
            "type": "SNAPSHOT",
            "ts": 1000,
            "symbol": "BTCUSDT",
            "bid_price": "50000.00",
            "ask_price": "50001.00",
            "bid_qty": "1.0",
            "ask_qty": "1.0",
            "last_price": "50000.50",
            "last_qty": "0.5",
        },
        {
            "type": "SNAPSHOT",
            "ts": 1000,  # Duplicate ts
            "symbol": "BTCUSDT",
            "bid_price": "50002.00",
            "ask_price": "50003.00",
            "bid_qty": "2.0",
            "ask_qty": "2.0",
            "last_price": "50002.50",
            "last_qty": "1.0",
        },
        {
            "type": "SNAPSHOT",
            "ts": 2000,
            "symbol": "BTCUSDT",
            "bid_price": "50100.00",
            "ask_price": "50101.00",
            "bid_qty": "1.5",
            "ask_qty": "1.5",
            "last_price": "50100.50",
            "last_qty": "0.3",
        },
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        jsonl_path = path / "events.jsonl"
        with jsonl_path.open("w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        yield path


# --- RetryConfig Tests ---


class TestRetryConfig:
    """Tests for RetryConfig delay calculations."""

    def test_first_attempt_uses_base_delay(self) -> None:
        """First attempt (0) uses base delay."""
        config = RetryConfig(base_delay_ms=1000, backoff_multiplier=2.0)
        assert config.get_delay_ms(0) == 1000

    def test_exponential_backoff(self) -> None:
        """Delays increase exponentially."""
        config = RetryConfig(base_delay_ms=1000, backoff_multiplier=2.0, max_delay_ms=60000)
        assert config.get_delay_ms(0) == 1000
        assert config.get_delay_ms(1) == 2000
        assert config.get_delay_ms(2) == 4000
        assert config.get_delay_ms(3) == 8000

    def test_delay_caps_at_max(self) -> None:
        """Delay is capped at max_delay_ms."""
        config = RetryConfig(base_delay_ms=1000, backoff_multiplier=2.0, max_delay_ms=5000)
        assert config.get_delay_ms(10) == 5000  # Would be 1024000 without cap

    def test_negative_attempt_returns_base(self) -> None:
        """Negative attempt numbers return base delay."""
        config = RetryConfig(base_delay_ms=1000)
        assert config.get_delay_ms(-1) == 1000

    def test_linear_backoff(self) -> None:
        """Multiplier of 1.0 gives linear (constant) delay."""
        config = RetryConfig(base_delay_ms=1000, backoff_multiplier=1.0)
        assert config.get_delay_ms(0) == 1000
        assert config.get_delay_ms(5) == 1000


# --- BinanceWsMockConnector Tests ---


class TestBinanceWsMockConnectorLifecycle:
    """Tests for connector lifecycle management."""

    @pytest.mark.asyncio
    async def test_initial_state_is_disconnected(self, fixture_path: Path) -> None:
        """Connector starts in DISCONNECTED state."""
        connector = BinanceWsMockConnector(fixture_path)
        assert connector.state == ConnectorState.DISCONNECTED
        assert connector.last_seen_ts is None

    @pytest.mark.asyncio
    async def test_connect_changes_state(self, fixture_path: Path) -> None:
        """Connect transitions to CONNECTED state."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED
        await connector.close()

    @pytest.mark.asyncio
    async def test_close_changes_state(self, fixture_path: Path) -> None:
        """Close transitions to CLOSED state."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()
        await connector.close()
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, fixture_path: Path) -> None:
        """Close can be called multiple times safely."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()
        await connector.close()
        await connector.close()  # Should not raise
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_connect_when_already_connected(self, fixture_path: Path) -> None:
        """Connect is no-op if already connected."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()
        await connector.connect()  # Should not raise
        assert connector.state == ConnectorState.CONNECTED
        await connector.close()

    @pytest.mark.asyncio
    async def test_connect_missing_fixture_raises(self) -> None:
        """Connect raises ConnectionError if fixture not found."""
        connector = BinanceWsMockConnector(Path("/nonexistent/path"))
        with pytest.raises(ConnectionError, match="Fixture not found"):
            await connector.connect()
        assert connector.state == ConnectorState.DISCONNECTED


class TestBinanceWsMockConnectorIteration:
    """Tests for snapshot iteration."""

    @pytest.mark.asyncio
    async def test_iterates_all_snapshots(self, fixture_path: Path) -> None:
        """All SNAPSHOT events are yielded."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        snapshots: list[Snapshot] = []
        async for snapshot in connector.iter_snapshots():
            snapshots.append(snapshot)

        assert len(snapshots) == 3
        await connector.close()

    @pytest.mark.asyncio
    async def test_snapshots_in_timestamp_order(self, fixture_path: Path) -> None:
        """Snapshots are yielded in timestamp order."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        timestamps: list[int] = []
        async for snapshot in connector.iter_snapshots():
            timestamps.append(snapshot.ts)

        assert timestamps == sorted(timestamps)
        await connector.close()

    @pytest.mark.asyncio
    async def test_snapshot_values_parsed_correctly(self, fixture_path: Path) -> None:
        """Snapshot fields are parsed correctly."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        snapshots: list[Snapshot] = []
        async for snapshot in connector.iter_snapshots():
            snapshots.append(snapshot)

        first = snapshots[0]
        assert first.ts == 1000
        assert first.symbol == "BTCUSDT"
        assert first.bid_price == Decimal("50000.00")
        assert first.ask_price == Decimal("50001.00")
        await connector.close()

    @pytest.mark.asyncio
    async def test_iteration_without_connect_raises(self, fixture_path: Path) -> None:
        """Iteration without connect raises ConnectionError."""
        connector = BinanceWsMockConnector(fixture_path)

        with pytest.raises(ConnectionError, match="connector state is disconnected"):
            async for _ in connector.iter_snapshots():
                pass

    @pytest.mark.asyncio
    async def test_last_seen_ts_updated(self, fixture_path: Path) -> None:
        """last_seen_ts is updated after each snapshot."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        assert connector.last_seen_ts is None

        async for snapshot in connector.iter_snapshots():
            assert connector.last_seen_ts == snapshot.ts

        assert connector.last_seen_ts == 3000
        await connector.close()


class TestBinanceWsMockConnectorIdempotency:
    """Tests for idempotency (no duplicate timestamps)."""

    @pytest.mark.asyncio
    async def test_skips_duplicate_timestamps(self, fixture_path_with_duplicates: Path) -> None:
        """Duplicate timestamps are skipped."""
        connector = BinanceWsMockConnector(fixture_path_with_duplicates)
        await connector.connect()

        timestamps: list[int] = []
        async for snapshot in connector.iter_snapshots():
            timestamps.append(snapshot.ts)

        # First ts=1000 is kept, second ts=1000 is skipped
        assert timestamps == [1000, 2000]
        assert connector.stats.duplicates_skipped == 1
        await connector.close()

    @pytest.mark.asyncio
    async def test_reconnect_resumes_from_last_seen(self, fixture_path: Path) -> None:
        """Reconnect resumes from last_seen_ts."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        # Read first snapshot
        count = 0
        async for _snapshot in connector.iter_snapshots():
            count += 1
            if count == 1:
                break

        assert connector.last_seen_ts == 1000

        # Reconnect
        await connector.reconnect()
        assert connector.state == ConnectorState.CONNECTED

        # Should resume from ts > 1000
        remaining: list[int] = []
        async for snapshot in connector.iter_snapshots():
            remaining.append(snapshot.ts)

        assert remaining == [2000, 3000]
        await connector.close()


class TestBinanceWsMockConnectorSymbolFilter:
    """Tests for symbol filtering."""

    @pytest.mark.asyncio
    async def test_filter_single_symbol(self, fixture_path: Path) -> None:
        """Filter to single symbol."""
        connector = BinanceWsMockConnector(fixture_path, symbols=["BTCUSDT"])
        await connector.connect()

        symbols: list[str] = []
        async for snapshot in connector.iter_snapshots():
            symbols.append(snapshot.symbol)

        assert all(s == "BTCUSDT" for s in symbols)
        assert len(symbols) == 2  # Only BTC snapshots
        await connector.close()

    @pytest.mark.asyncio
    async def test_filter_multiple_symbols(self, fixture_path: Path) -> None:
        """Filter to multiple symbols."""
        connector = BinanceWsMockConnector(fixture_path, symbols=["BTCUSDT", "ETHUSDT"])
        await connector.connect()

        count = 0
        async for _ in connector.iter_snapshots():
            count += 1

        assert count == 3  # All snapshots pass filter
        await connector.close()

    @pytest.mark.asyncio
    async def test_filter_nonexistent_symbol(self, fixture_path: Path) -> None:
        """Filter to nonexistent symbol yields nothing."""
        connector = BinanceWsMockConnector(fixture_path, symbols=["XYZUSDT"])
        await connector.connect()

        count = 0
        async for _ in connector.iter_snapshots():
            count += 1

        assert count == 0
        await connector.close()


class TestBinanceWsMockConnectorStats:
    """Tests for statistics tracking."""

    @pytest.mark.asyncio
    async def test_stats_events_loaded(self, fixture_path: Path) -> None:
        """Stats track events loaded."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()
        assert connector.stats.events_loaded == 3
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_snapshots_delivered(self, fixture_path: Path) -> None:
        """Stats track snapshots delivered."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        async for _ in connector.iter_snapshots():
            pass

        assert connector.stats.snapshots_delivered == 3
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_duplicates_skipped(self, fixture_path_with_duplicates: Path) -> None:
        """Stats track duplicates skipped."""
        connector = BinanceWsMockConnector(fixture_path_with_duplicates)
        await connector.connect()

        async for _ in connector.iter_snapshots():
            pass

        assert connector.stats.duplicates_skipped == 1
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_reconnect_attempts(self, fixture_path: Path) -> None:
        """Stats track reconnect attempts."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        await connector.reconnect()
        await connector.reconnect()

        assert connector.stats.reconnect_attempts == 2
        await connector.close()

    @pytest.mark.asyncio
    async def test_reset_clears_state(self, fixture_path: Path) -> None:
        """Reset clears connector state for reuse."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        # Consume some snapshots
        count = 0
        async for _ in connector.iter_snapshots():
            count += 1
            if count == 2:
                break

        assert connector.last_seen_ts == 2000
        assert connector.stats.snapshots_delivered == 2

        # Reset
        connector.reset()

        assert connector.last_seen_ts is None
        assert connector.stats.snapshots_delivered == 0
        assert connector.stats.events_loaded == 3  # Events still loaded

        # Can iterate from start again
        all_ts: list[int] = []
        async for snapshot in connector.iter_snapshots():
            all_ts.append(snapshot.ts)

        assert all_ts == [1000, 2000, 3000]
        await connector.close()


class TestBinanceWsMockConnectorReadDelay:
    """Tests for read delay simulation."""

    @pytest.mark.asyncio
    async def test_read_delay_slows_iteration(self, fixture_path: Path) -> None:
        """Read delay adds time between snapshots."""
        connector = BinanceWsMockConnector(fixture_path, read_delay_ms=50)
        await connector.connect()

        start = asyncio.get_event_loop().time()
        count = 0
        async for _ in connector.iter_snapshots():
            count += 1

        elapsed = asyncio.get_event_loop().time() - start

        # 3 snapshots with 50ms delay each = ~150ms minimum
        assert elapsed >= 0.1  # Allow some margin
        assert count == 3
        await connector.close()

    @pytest.mark.asyncio
    async def test_zero_delay_is_fast(self, fixture_path: Path) -> None:
        """Zero delay processes snapshots quickly."""
        connector = BinanceWsMockConnector(fixture_path, read_delay_ms=0)
        await connector.connect()

        start = asyncio.get_event_loop().time()
        async for _ in connector.iter_snapshots():
            pass
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 0.1  # Should be very fast
        await connector.close()
