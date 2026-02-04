"""Integration tests for paper write-path (M3-LC-02).

Tests verify end-to-end behavior:
- READ_ONLY mode blocks write operations
- PAPER mode allows write operations without network calls
- Order lifecycle (place -> replace flow)
- Deterministic behavior across operations
- Bounded-time execution
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from grinder.connectors import (
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorState,
    LiveConnectorConfig,
    LiveConnectorV0,
    PaperOrderError,
    SafeMode,
    reset_connector_metrics,
)
from grinder.core import OrderSide, OrderState


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
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start_time=1000.0)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset metrics before each test."""
    reset_connector_metrics()


# --- SafeMode Enforcement Tests ---


class TestSafeModeEnforcement:
    """Tests for SafeMode enforcement on write operations."""

    @pytest.mark.asyncio
    async def test_read_only_blocks_place_order(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode should block place_order."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        with pytest.raises(ConnectorNonRetryableError, match="SafeMode violation"):
            connector.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )

        await connector.close()

    @pytest.mark.asyncio
    async def test_read_only_blocks_cancel_order(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode should block cancel_order."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        with pytest.raises(ConnectorNonRetryableError, match="SafeMode violation"):
            connector.cancel_order("PAPER_00000001")

        await connector.close()

    @pytest.mark.asyncio
    async def test_read_only_blocks_replace_order(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode should block replace_order."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        with pytest.raises(ConnectorNonRetryableError, match="SafeMode violation"):
            connector.replace_order("PAPER_00000001", new_price=Decimal("60000"))

        await connector.close()

    @pytest.mark.asyncio
    async def test_paper_mode_allows_place_order(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode should allow place_order."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )

        assert result.order_id.startswith("PAPER_")
        assert result.state == OrderState.FILLED  # V0 instant fill

        await connector.close()


# --- Order Lifecycle Tests ---


class TestOrderLifecycle:
    """Tests for order lifecycle in PAPER mode."""

    @pytest.mark.asyncio
    async def test_place_order_returns_deterministic_id(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """place_order should return deterministic order IDs."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result1 = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )
        result2 = connector.place_order(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            price=Decimal("3000"),
            quantity=Decimal("10"),
        )

        assert result1.order_id == "PAPER_00000001"
        assert result2.order_id == "PAPER_00000002"

        await connector.close()

    @pytest.mark.asyncio
    async def test_place_order_instant_fill(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """V0: Orders should be instantly filled."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1.5"),
        )

        assert result.state == OrderState.FILLED
        assert result.filled_quantity == Decimal("1.5")

        await connector.close()

    @pytest.mark.asyncio
    async def test_cancel_filled_order_raises_error(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Cancelling a filled order should raise error."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )

        # V0: Order is instantly filled, can't cancel
        with pytest.raises(PaperOrderError, match="Cannot cancel filled order"):
            connector.cancel_order(result.order_id)

        await connector.close()

    @pytest.mark.asyncio
    async def test_replace_filled_order_raises_error(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Replacing a filled order should raise error."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()

        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )

        # V0: Order is instantly filled, can't replace
        with pytest.raises(PaperOrderError, match="Cannot replace order"):
            connector.replace_order(result.order_id, new_price=Decimal("55000"))

        await connector.close()


# --- No Network Calls Tests ---


class TestNoNetworkCalls:
    """Tests verifying PAPER mode makes no network calls."""

    @pytest.mark.asyncio
    async def test_paper_mode_no_ws_connection(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode should not establish real WebSocket connection."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(
                mode=SafeMode.PAPER,
                # Use a URL that would fail if accessed
                ws_url="wss://nonexistent.invalid/ws",
            ),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        # Should connect successfully (mock connection)
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED

        # Should place order without network
        result = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )
        assert result.order_id.startswith("PAPER_")

        await connector.close()

    @pytest.mark.asyncio
    async def test_paper_adapter_is_initialized(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """PAPER mode connector should have paper_adapter initialized."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        assert connector.paper_adapter is not None

    @pytest.mark.asyncio
    async def test_read_only_no_paper_adapter(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """READ_ONLY mode connector should not have paper_adapter."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.READ_ONLY),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        assert connector.paper_adapter is None


# --- Bounded-Time Tests ---


class TestBoundedTime:
    """Tests verifying bounded-time execution."""

    @pytest.mark.asyncio
    async def test_order_operations_are_instant(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Order operations should complete nearly instantly."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        start = time.time()

        await connector.connect()

        # Place multiple orders
        for _ in range(100):
            connector.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )

        await connector.close()

        elapsed = time.time() - start

        # Should complete in < 1 second even with 100 orders
        assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_full_lifecycle_bounded_time(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Full lifecycle should complete in bounded time."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )

        start = time.time()

        # Connect
        await connector.connect()

        # Place orders
        result1 = connector.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )
        result2 = connector.place_order(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            price=Decimal("3000"),
            quantity=Decimal("5"),
        )

        # Stream ticks (empty in v0 mock)
        async for _ in connector.stream_ticks():
            pass

        # Close
        await connector.close()

        elapsed = time.time() - start

        assert elapsed < 1.0
        assert result1.order_id == "PAPER_00000001"
        assert result2.order_id == "PAPER_00000002"


# --- Determinism Tests ---


class TestDeterminism:
    """Tests verifying deterministic behavior."""

    @pytest.mark.asyncio
    async def test_same_sequence_same_ids(self) -> None:
        """Same operation sequence should produce same order IDs."""
        # Run 1
        connector1 = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=FakeClock(start_time=1000.0),
            sleep_func=FakeSleep(),
        )
        await connector1.connect()
        r1_1 = connector1.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )
        r1_2 = connector1.place_order(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            price=Decimal("3000"),
            quantity=Decimal("5"),
        )
        await connector1.close()

        # Run 2 (same sequence)
        connector2 = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=FakeClock(start_time=1000.0),
            sleep_func=FakeSleep(),
        )
        await connector2.connect()
        r2_1 = connector2.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("1"),
        )
        r2_2 = connector2.place_order(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            price=Decimal("3000"),
            quantity=Decimal("5"),
        )
        await connector2.close()

        # Same IDs
        assert r1_1.order_id == r2_1.order_id == "PAPER_00000001"
        assert r1_2.order_id == r2_2.order_id == "PAPER_00000002"

        # Same timestamps (from same clock)
        assert r1_1.created_ts == r2_1.created_ts
        assert r1_2.created_ts == r2_2.created_ts


# --- Error Handling Tests ---


class TestErrorHandling:
    """Tests for error handling in paper write-path."""

    @pytest.mark.asyncio
    async def test_place_order_on_closed_connector(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Should raise error when placing order on closed connector."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError):
            connector.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("1"),
            )

    @pytest.mark.asyncio
    async def test_cancel_order_on_closed_connector(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Should raise error when cancelling order on closed connector."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError):
            connector.cancel_order("PAPER_00000001")

    @pytest.mark.asyncio
    async def test_replace_order_on_closed_connector(
        self, fake_clock: FakeClock, fake_sleep: FakeSleep
    ) -> None:
        """Should raise error when replacing order on closed connector."""
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(mode=SafeMode.PAPER),
            clock=fake_clock,
            sleep_func=fake_sleep,
        )
        await connector.connect()
        await connector.close()

        with pytest.raises(ConnectorClosedError):
            connector.replace_order("PAPER_00000001", new_price=Decimal("55000"))
