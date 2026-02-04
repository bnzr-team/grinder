"""Integration tests for LiveConnectorV0.

Tests verify end-to-end behavior with bounded-time execution:
- Connector lifecycle across multiple operations
- H2/H4/H5 hardening in realistic scenarios
- SafeMode enforcement in workflows
"""

from __future__ import annotations

import time

import pytest

from grinder.connectors import (
    CircuitState,
    ConnectorNonRetryableError,
    ConnectorState,
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
    reset_connector_metrics,
)
from grinder.connectors.circuit_breaker import CircuitBreakerConfig
from grinder.connectors.retries import RetryPolicy


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


@pytest.mark.integration
class TestLiveConnectorIntegration:
    """Integration tests for LiveConnectorV0."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_workflow(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Test complete lifecycle: connect -> stream -> reconnect -> close."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.READ_ONLY,
                symbols=["BTCUSDT"],
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        # Phase 1: Connect
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED
        assert connector.mode == SafeMode.READ_ONLY

        # Phase 2: Stream (v0 yields nothing but should work)
        async for _ in connector.stream_ticks():
            pass

        # Phase 3: Subscribe more symbols
        await connector.subscribe(["ETHUSDT"])
        assert "ETHUSDT" in connector.symbols

        # Phase 4: Reconnect
        await connector.reconnect()
        assert connector.state == ConnectorState.CONNECTED
        assert connector.stats.reconnections == 1

        # Phase 5: Close
        await connector.close()
        assert connector.state == ConnectorState.CLOSED  # type: ignore[comparison-overlap]

    @pytest.mark.asyncio
    async def test_multi_symbol_subscription_workflow(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Test subscribing to multiple symbols over time."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"]),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        await connector.connect()

        # Initial symbols
        assert connector.symbols == ["BTCUSDT"]

        # Add more symbols in batches
        await connector.subscribe(["ETHUSDT"])
        assert set(connector.symbols) == {"BTCUSDT", "ETHUSDT"}

        await connector.subscribe(["SOLUSDT", "AVAXUSDT"])
        assert set(connector.symbols) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"}

        # Duplicate subscribe should not add duplicates
        await connector.subscribe(["BTCUSDT"])
        assert connector.symbols.count("BTCUSDT") == 1

        await connector.close()

    @pytest.mark.asyncio
    async def test_safe_mode_upgrade_path(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Test that safe mode properly controls operations."""
        # Start with READ_ONLY (safest)
        connector_ro = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector_ro.connect()

        # READ_ONLY allows streaming
        async for _ in connector_ro.stream_ticks():
            pass

        # But blocks paper operations
        with pytest.raises(ConnectorNonRetryableError):
            connector_ro.assert_mode(SafeMode.PAPER)

        await connector_ro.close()

        # PAPER mode connector
        connector_paper = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector_paper.connect()

        # PAPER allows paper operations
        connector_paper.assert_mode(SafeMode.READ_ONLY)
        connector_paper.assert_mode(SafeMode.PAPER)

        # But blocks live trading
        with pytest.raises(ConnectorNonRetryableError):
            connector_paper.assert_mode(SafeMode.LIVE_TRADE)

        await connector_paper.close()

    @pytest.mark.asyncio
    async def test_circuit_breaker_recovery_workflow(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Test circuit breaker tripping and recovery."""
        config = LiveConnectorConfig(
            circuit_breaker_config=CircuitBreakerConfig(
                failure_threshold=2,
                open_interval_s=30.0,
            ),
        )
        connector = LiveConnectorV0(
            config=config,
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        # First connect succeeds
        await connector.connect()
        assert connector.circuit_breaker.state("connect") == CircuitState.CLOSED
        await connector.close()

        # Manually trip the circuit (simulating failures)
        connector._state = ConnectorState.DISCONNECTED  # Reset state for test
        connector.circuit_breaker.record_failure("connect", "test1")
        connector.circuit_breaker.record_failure("connect", "test2")

        # Circuit should be OPEN
        assert connector.circuit_breaker.state("connect") == CircuitState.OPEN

        # Advance time past open_interval
        fake_clock.advance(35.0)

        # Circuit should transition to HALF_OPEN
        assert connector.circuit_breaker.state("connect") == CircuitState.HALF_OPEN

        # Success should close the circuit
        connector.circuit_breaker.record_success("connect")
        assert connector.circuit_breaker.state("connect") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_bounded_time_guarantee(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Test that all operations complete in bounded time (no real sleeps)."""
        # Configure retry policy with delays that would be slow in real time
        config = LiveConnectorConfig(
            retry_policy=RetryPolicy(
                max_attempts=5,
                base_delay_ms=1000,  # 1 second delays
                max_delay_ms=10000,  # Up to 10 second delays
            ),
        )
        connector = LiveConnectorV0(
            config=config,
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        start = time.time()

        # All these operations should complete nearly instantly
        await connector.connect()
        await connector.reconnect()
        await connector.reconnect()
        await connector.close()

        elapsed = time.time() - start

        # Should complete in < 1 second real time despite retry policy
        assert elapsed < 1.0

        # But fake sleep should have recorded some sleep calls from retries/connect
        # At least the connect simulates a small sleep
        assert len(fake_sleep.calls) > 0

    @pytest.mark.asyncio
    async def test_stats_accumulation_across_operations(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Test that stats accumulate correctly across operations."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"]),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        # Connect
        await connector.connect()
        assert connector.stats.connection_attempts == 1

        # Multiple reconnects
        for _ in range(3):
            await connector.reconnect()

        assert connector.stats.reconnections == 3

        # Close
        await connector.close()

        # Stats should reflect all operations
        assert connector.stats.connection_attempts == 1
        assert connector.stats.reconnections == 3
