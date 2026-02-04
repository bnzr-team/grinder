"""Unit tests for LiveConnectorV0.

Tests cover:
- Connector lifecycle (connect/close)
- SafeMode enforcement
- H2/H4/H5 hardening (retries, circuit breaker, metrics)
- Bounded-time testing with injectable clock/sleep
- Error handling
- Statistics tracking
"""

from __future__ import annotations

import time

import pytest

from grinder.connectors import (
    CircuitOpenError,
    CircuitState,
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorState,
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
    reset_connector_metrics,
)
from grinder.connectors.circuit_breaker import CircuitBreakerConfig


class FakeClock:
    """Fake clock for bounded-time testing."""

    def __init__(self, start_time: float = 0.0) -> None:
        self._time = start_time

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class FakeSleep:
    """Fake sleep for bounded-time testing."""

    def __init__(self) -> None:
        self.total_slept: float = 0.0
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.total_slept += seconds
        self.calls.append(seconds)
        # No actual sleep - instant return


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset connector metrics before each test."""
    reset_connector_metrics()


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide fake clock for tests."""
    return FakeClock()


@pytest.fixture
def fake_sleep() -> FakeSleep:
    """Provide fake sleep for tests."""
    return FakeSleep()


# --- SafeMode Tests ---


class TestSafeMode:
    """Tests for SafeMode enum."""

    def test_safe_mode_values(self) -> None:
        """SafeMode has correct values."""
        assert SafeMode.READ_ONLY.value == "read_only"
        assert SafeMode.PAPER.value == "paper"
        assert SafeMode.LIVE_TRADE.value == "live_trade"

    def test_default_mode_is_read_only(self) -> None:
        """Default config mode is READ_ONLY."""
        config = LiveConnectorConfig()
        assert config.mode == SafeMode.READ_ONLY

    @pytest.mark.asyncio
    async def test_connector_mode_property(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connector exposes mode property."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        assert connector.mode == SafeMode.PAPER


# --- Lifecycle Tests ---


class TestLiveConnectorLifecycle:
    """Tests for connector lifecycle management."""

    @pytest.mark.asyncio
    async def test_initial_state_is_disconnected(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connector starts in DISCONNECTED state."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        assert connector.state == ConnectorState.DISCONNECTED
        assert connector.last_seen_ts is None

    @pytest.mark.asyncio
    async def test_connect_changes_state(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connect transitions to CONNECTED state."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED
        await connector.close()

    @pytest.mark.asyncio
    async def test_close_changes_state(self, fake_clock: FakeClock, fake_sleep: FakeSleep) -> None:
        """Close transitions to CLOSED state."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, fake_clock: FakeClock, fake_sleep: FakeSleep) -> None:
        """Close can be called multiple times safely."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()
        await connector.close()  # Should not raise
        assert connector.state == ConnectorState.CLOSED

    @pytest.mark.asyncio
    async def test_connect_when_already_connected(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connect is no-op if already connected."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.connect()  # Should not raise
        assert connector.state == ConnectorState.CONNECTED
        assert connector.stats.connection_attempts == 1  # Only counted once
        await connector.close()


# --- Stream Tests ---


class TestLiveConnectorStream:
    """Tests for streaming methods."""

    @pytest.mark.asyncio
    async def test_stream_ticks_without_connect_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """stream_ticks without connect raises ConnectionError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        with pytest.raises(ConnectionError, match="connector state is disconnected"):
            async for _ in connector.stream_ticks():
                pass

    @pytest.mark.asyncio
    async def test_stream_ticks_after_close_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """stream_ticks after close raises ConnectorClosedError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError) as exc_info:
            async for _ in connector.stream_ticks():
                pass

        assert exc_info.value.op == "stream_ticks"

    @pytest.mark.asyncio
    async def test_iter_snapshots_is_alias(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """iter_snapshots is alias for stream_ticks."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()

        # Both should work identically (v0 yields nothing)
        async for _ in connector.iter_snapshots():
            pass
        async for _ in connector.stream_ticks():
            pass

        await connector.close()


# --- Subscribe Tests ---


class TestLiveConnectorSubscribe:
    """Tests for subscription management."""

    @pytest.mark.asyncio
    async def test_subscribe_adds_symbols(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """subscribe adds symbols to config."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"]),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        await connector.subscribe(["ETHUSDT", "SOLUSDT"])

        assert "BTCUSDT" in connector.symbols
        assert "ETHUSDT" in connector.symbols
        assert "SOLUSDT" in connector.symbols
        await connector.close()

    @pytest.mark.asyncio
    async def test_subscribe_without_connect_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """subscribe without connect raises ConnectionError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        with pytest.raises(ConnectionError, match="connector state is disconnected"):
            await connector.subscribe(["BTCUSDT"])

    @pytest.mark.asyncio
    async def test_subscribe_after_close_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """subscribe after close raises ConnectorClosedError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError) as exc_info:
            await connector.subscribe(["BTCUSDT"])

        assert exc_info.value.op == "subscribe"


# --- Reconnect Tests ---


class TestLiveConnectorReconnect:
    """Tests for reconnection behavior."""

    @pytest.mark.asyncio
    async def test_reconnect_increments_stats(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """reconnect increments reconnection stats."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()

        await connector.reconnect()
        await connector.reconnect()

        assert connector.stats.reconnections == 2
        assert connector.state == ConnectorState.CONNECTED
        await connector.close()

    @pytest.mark.asyncio
    async def test_reconnect_after_close_raises(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """reconnect after close raises ConnectorClosedError."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError) as exc_info:
            await connector.reconnect()

        assert exc_info.value.op == "reconnect"


# --- SafeMode Enforcement Tests ---


class TestSafeModeEnforcement:
    """Tests for safe mode enforcement."""

    @pytest.mark.asyncio
    async def test_assert_mode_read_only_allows_read_only(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode allows READ_ONLY operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        connector.assert_mode(SafeMode.READ_ONLY)  # Should not raise

    @pytest.mark.asyncio
    async def test_assert_mode_read_only_blocks_paper(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode blocks PAPER operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        with pytest.raises(ConnectorNonRetryableError, match="requires mode=paper"):
            connector.assert_mode(SafeMode.PAPER)

    @pytest.mark.asyncio
    async def test_assert_mode_paper_allows_paper(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode allows PAPER operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        connector.assert_mode(SafeMode.PAPER)  # Should not raise

    @pytest.mark.asyncio
    async def test_assert_mode_paper_blocks_live_trade(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode blocks LIVE_TRADE operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        with pytest.raises(ConnectorNonRetryableError, match="requires mode=live_trade"):
            connector.assert_mode(SafeMode.LIVE_TRADE)

    @pytest.mark.asyncio
    async def test_assert_mode_live_trade_allows_all(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """LIVE_TRADE mode allows all operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.LIVE_TRADE),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        connector.assert_mode(SafeMode.READ_ONLY)  # Should not raise
        connector.assert_mode(SafeMode.PAPER)  # Should not raise
        connector.assert_mode(SafeMode.LIVE_TRADE)  # Should not raise


# --- H4 Circuit Breaker Tests ---


class TestLiveConnectorCircuitBreaker:
    """Tests for circuit breaker integration (H4)."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_exposed(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Circuit breaker is accessible for monitoring."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        assert connector.circuit_breaker is not None
        assert connector.circuit_breaker.state("connect") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_on_failures(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Circuit breaker trips after threshold failures."""
        config = LiveConnectorConfig(
            circuit_breaker_config=CircuitBreakerConfig(
                failure_threshold=2,  # Trip after 2 failures
            ),
        )
        connector = LiveConnectorV0(
            config=config,
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        # Manually record failures to trip the breaker
        connector.circuit_breaker.record_failure("connect", "test")
        connector.circuit_breaker.record_failure("connect", "test")

        # Circuit should be OPEN now
        assert connector.circuit_breaker.state("connect") == CircuitState.OPEN

        # connect() should fail with CircuitOpenError
        with pytest.raises(CircuitOpenError):
            await connector.connect()


# --- Statistics Tests ---


class TestLiveConnectorStats:
    """Tests for statistics tracking."""

    @pytest.mark.asyncio
    async def test_stats_connection_attempts(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Stats track connection attempts."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        assert connector.stats.connection_attempts == 1
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_reconnections(self, fake_clock: FakeClock, fake_sleep: FakeSleep) -> None:
        """Stats track reconnection attempts."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()
        await connector.reconnect()
        assert connector.stats.reconnections == 1
        await connector.close()

    @pytest.mark.asyncio
    async def test_stats_errors_list_accessible(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Stats errors list is accessible and modifiable."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)
        await connector.connect()

        # Errors list is empty by default on successful connect
        assert isinstance(connector.stats.errors, list)

        await connector.close()


# --- Configuration Tests ---


class TestLiveConnectorConfig:
    """Tests for configuration."""

    def test_default_config_values(self) -> None:
        """Default config has safe values."""
        config = LiveConnectorConfig()
        assert config.mode == SafeMode.READ_ONLY
        assert config.symbols == []
        assert "testnet" in config.ws_url  # Safe testnet URL by default

    def test_config_with_symbols(self) -> None:
        """Config accepts symbol list."""
        config = LiveConnectorConfig(symbols=["BTCUSDT", "ETHUSDT"])
        assert "BTCUSDT" in config.symbols
        assert "ETHUSDT" in config.symbols

    @pytest.mark.asyncio
    async def test_connector_uses_config(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Connector uses provided config."""
        config = LiveConnectorConfig(
            mode=SafeMode.PAPER,
            symbols=["BTCUSDT"],
        )
        connector = LiveConnectorV0(
            config=config,
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        assert connector.mode == SafeMode.PAPER
        assert "BTCUSDT" in connector.symbols


# --- Bounded-Time Testing ---


class TestBoundedTimeTesting:
    """Tests for bounded-time behavior with injectable clock/sleep."""

    @pytest.mark.asyncio
    async def test_fake_sleep_no_actual_delay(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Fake sleep causes no actual delay."""
        connector = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        start = time.time()
        await connector.connect()
        elapsed = time.time() - start

        # Should be nearly instant (< 100ms) even though connect has a small sleep
        assert elapsed < 0.1
        assert fake_sleep.total_slept > 0  # But fake sleep was called
        await connector.close()

    @pytest.mark.asyncio
    async def test_fake_clock_controllable(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Fake clock allows time control."""
        _ = LiveConnectorV0(clock=fake_clock, sleep_func=fake_sleep)

        assert fake_clock.time() == 0.0
        fake_clock.advance(10.0)
        assert fake_clock.time() == 10.0
