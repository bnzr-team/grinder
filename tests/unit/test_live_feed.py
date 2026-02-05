"""Tests for live feed pipeline (LC-06).

This module tests the read-only data pipeline:
- WebSocket → Snapshot → FeatureEngine → LiveFeaturesUpdate

CRITICAL: test_no_execution_imports verifies that feed.py has ZERO
imports from execution/ module, enforcing the read-only constraint.

See ADR-037 for design decisions.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from grinder.connectors.binance_ws import (
    BinanceWsConfig,
    BinanceWsConnector,
    FakeWsTransport,
)
from grinder.connectors.errors import ConnectorClosedError, ConnectorTransientError
from grinder.contracts import Snapshot
from grinder.features.engine import FeatureEngineConfig
from grinder.live import (
    BookTickerData,
    LiveFeaturesUpdate,
    LiveFeed,
    LiveFeedConfig,
    WsMessage,
)

# --- P0: Hard-block test for read-only constraint ---


class TestNoExecutionImports:
    """Tests ensuring feed.py has no execution imports (read-only)."""

    def test_feed_py_has_no_execution_imports(self) -> None:
        """feed.py must NOT import anything from grinder.execution.

        This is the hard-block test ensuring the read-path is truly read-only.
        """
        feed_path = Path(__file__).parent.parent.parent / "src" / "grinder" / "live" / "feed.py"
        assert feed_path.exists(), f"feed.py not found at {feed_path}"

        source = feed_path.read_text()
        tree = ast.parse(source)

        execution_imports: list[str] = []

        for node in ast.walk(tree):
            # Check import statements: import grinder.execution
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "execution" in alias.name:
                        execution_imports.append(alias.name)

            # Check from ... import statements
            if isinstance(node, ast.ImportFrom):
                # Case 1: from grinder.execution import X
                if node.module and "execution" in node.module:
                    execution_imports.append(node.module)
                # Case 2: from grinder import execution
                if node.module and node.module.startswith("grinder"):
                    for alias in node.names:
                        if alias.name == "execution" or alias.name.startswith("execution."):
                            execution_imports.append(f"{node.module}.{alias.name}")

        assert not execution_imports, (
            f"feed.py must not import from execution module! Found imports: {execution_imports}"
        )

    def test_types_py_has_no_execution_imports(self) -> None:
        """types.py must NOT import anything from grinder.execution."""
        types_path = Path(__file__).parent.parent.parent / "src" / "grinder" / "live" / "types.py"
        assert types_path.exists(), f"types.py not found at {types_path}"

        source = types_path.read_text()
        tree = ast.parse(source)

        execution_imports: list[str] = []

        for node in ast.walk(tree):
            # Check import statements: import grinder.execution
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "execution" in alias.name:
                        execution_imports.append(alias.name)

            # Check from ... import statements
            if isinstance(node, ast.ImportFrom):
                # Case 1: from grinder.execution import X
                if node.module and "execution" in node.module:
                    execution_imports.append(node.module)
                # Case 2: from grinder import execution
                if node.module and node.module.startswith("grinder"):
                    for alias in node.names:
                        if alias.name == "execution" or alias.name.startswith("execution."):
                            execution_imports.append(f"{node.module}.{alias.name}")

        assert not execution_imports, (
            f"types.py must not import from execution module! Found imports: {execution_imports}"
        )


# --- Fixtures ---


def make_bookticker_message(
    symbol: str,
    bid_price: str,
    bid_qty: str,
    ask_price: str,
    ask_qty: str,
    update_id: int = 1,
) -> str:
    """Create a Binance bookTicker JSON message."""
    return json.dumps(
        {
            "u": update_id,
            "s": symbol,
            "b": bid_price,
            "B": bid_qty,
            "a": ask_price,
            "A": ask_qty,
        }
    )


@pytest.fixture
def sample_bookticker_messages() -> list[str]:
    """Sample bookTicker messages for testing."""
    return [
        make_bookticker_message("BTCUSDT", "50000.00", "1.5", "50001.00", "2.0", 1),
        make_bookticker_message("BTCUSDT", "50001.00", "1.0", "50002.00", "1.5", 2),
        make_bookticker_message("BTCUSDT", "50002.00", "2.0", "50003.00", "1.0", 3),
        make_bookticker_message("BTCUSDT", "50003.00", "1.5", "50004.00", "2.0", 4),
        make_bookticker_message("BTCUSDT", "50004.00", "1.0", "50005.00", "1.5", 5),
    ]


@pytest.fixture
def sample_snapshot() -> Snapshot:
    """Sample snapshot for testing."""
    return Snapshot(
        ts=1000000,
        symbol="BTCUSDT",
        bid_price=Decimal("50000.00"),
        ask_price=Decimal("50001.00"),
        bid_qty=Decimal("1.5"),
        ask_qty=Decimal("2.0"),
        last_price=Decimal("50000.50"),
        last_qty=Decimal("0.5"),
    )


# --- WsMessage and BookTickerData tests ---


class TestWsMessage:
    """Tests for WsMessage parsing."""

    def test_from_binance_bookticker(self) -> None:
        """Parse WsMessage from Binance bookTicker data."""
        data = {
            "u": 12345,
            "s": "BTCUSDT",
            "b": "50000.00",
            "B": "1.5",
            "a": "50001.00",
            "A": "2.0",
        }
        msg = WsMessage.from_binance_bookticker(data, recv_ts=1000000)

        assert msg.data == data
        assert msg.recv_ts == 1000000
        assert msg.stream == "btcusdt@bookTicker"


class TestBookTickerData:
    """Tests for BookTickerData parsing."""

    def test_from_ws_message(self) -> None:
        """Parse BookTickerData from WsMessage."""
        data = {
            "u": 12345,
            "s": "BTCUSDT",
            "b": "50000.00",
            "B": "1.5",
            "a": "50001.00",
            "A": "2.0",
        }
        msg = WsMessage(data=data, recv_ts=1000000)
        ticker = BookTickerData.from_ws_message(msg)

        assert ticker.symbol == "BTCUSDT"
        assert ticker.bid_price == Decimal("50000.00")
        assert ticker.bid_qty == Decimal("1.5")
        assert ticker.ask_price == Decimal("50001.00")
        assert ticker.ask_qty == Decimal("2.0")
        assert ticker.update_id == 12345

    def test_to_dict(self) -> None:
        """BookTickerData serializes correctly."""
        ticker = BookTickerData(
            symbol="BTCUSDT",
            bid_price=Decimal("50000.00"),
            bid_qty=Decimal("1.5"),
            ask_price=Decimal("50001.00"),
            ask_qty=Decimal("2.0"),
            update_id=12345,
        )
        d = ticker.to_dict()

        assert d["symbol"] == "BTCUSDT"
        assert d["bid_price"] == "50000.00"
        assert d["update_id"] == 12345


# --- FakeWsTransport tests ---


class TestFakeWsTransport:
    """Tests for fake WebSocket transport."""

    @pytest.mark.asyncio
    async def test_yields_messages_in_order(self) -> None:
        """FakeWsTransport yields messages in order."""
        messages = [
            make_bookticker_message("BTCUSDT", "50000", "1", "50001", "1"),
            make_bookticker_message("BTCUSDT", "50001", "1", "50002", "1"),
        ]
        transport = FakeWsTransport(messages=messages)

        await transport.connect("ws://test")

        msg1 = await transport.recv()
        msg2 = await transport.recv()

        assert "50000" in msg1
        assert "50001" in msg2

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self) -> None:
        """FakeWsTransport raises when not connected."""
        transport = FakeWsTransport(messages=[])

        with pytest.raises(ConnectorClosedError):
            await transport.recv()

    @pytest.mark.asyncio
    async def test_error_injection(self) -> None:
        """FakeWsTransport supports error injection."""
        messages = [
            make_bookticker_message("BTCUSDT", "50000", "1", "50001", "1"),
            make_bookticker_message("BTCUSDT", "50001", "1", "50002", "1"),
        ]
        transport = FakeWsTransport(messages=messages, error_after=1)

        await transport.connect("ws://test")

        # First message succeeds
        await transport.recv()

        # Second message raises error
        with pytest.raises(ConnectorTransientError):
            await transport.recv()


# --- BinanceWsConnector tests ---


class TestBinanceWsConnector:
    """Tests for BinanceWsConnector with fake transport."""

    @pytest.mark.asyncio
    async def test_connects_and_subscribes(self) -> None:
        """Connector connects and sends subscription message."""
        transport = FakeWsTransport(messages=[])
        config = BinanceWsConfig(symbols=["BTCUSDT"], use_testnet=True)
        connector = BinanceWsConnector(config, transport=transport)

        await connector.connect()

        assert connector.state.value == "connected"
        await connector.close()

    @pytest.mark.asyncio
    async def test_yields_snapshots(self) -> None:
        """Connector yields Snapshot objects from bookTicker messages."""
        messages = [
            make_bookticker_message("BTCUSDT", "50000.00", "1.5", "50001.00", "2.0"),
            make_bookticker_message("BTCUSDT", "50001.00", "1.0", "50002.00", "1.5"),
        ]
        transport = FakeWsTransport(messages=messages)
        config = BinanceWsConfig(symbols=["BTCUSDT"], use_testnet=True)

        # Use fake clock for deterministic timestamps
        ts = [1000000]

        def fake_clock() -> float:
            ts[0] += 1000
            return ts[0] / 1000.0

        connector = BinanceWsConnector(config, transport=transport, clock=fake_clock)

        await connector.connect()

        snapshots: list[Snapshot] = []
        count = 0
        async for snapshot in connector.iter_snapshots():
            snapshots.append(snapshot)
            count += 1
            if count >= 2:
                break

        await connector.close()

        assert len(snapshots) == 2
        assert snapshots[0].symbol == "BTCUSDT"
        assert snapshots[0].bid_price == Decimal("50000.00")
        assert snapshots[1].bid_price == Decimal("50001.00")

    @pytest.mark.asyncio
    async def test_skips_subscription_response(self) -> None:
        """Connector skips subscription response messages."""
        messages = [
            json.dumps({"result": None, "id": 1}),  # Subscription response
            make_bookticker_message("BTCUSDT", "50000.00", "1.5", "50001.00", "2.0"),
        ]
        transport = FakeWsTransport(messages=messages)
        config = BinanceWsConfig(symbols=["BTCUSDT"])
        connector = BinanceWsConnector(config, transport=transport)

        await connector.connect()

        snapshots: list[Snapshot] = []
        async for snapshot in connector.iter_snapshots():
            snapshots.append(snapshot)
            break  # Just get first real snapshot

        await connector.close()

        # Should only yield the bookTicker, not the subscription response
        assert len(snapshots) == 1
        assert snapshots[0].symbol == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_idempotency_skips_old_timestamps(self) -> None:
        """Connector skips snapshots with old/duplicate timestamps."""
        messages = [
            make_bookticker_message("BTCUSDT", "50000.00", "1.5", "50001.00", "2.0"),
            make_bookticker_message("BTCUSDT", "50001.00", "1.0", "50002.00", "1.5"),
        ]
        transport = FakeWsTransport(messages=messages)
        config = BinanceWsConfig(symbols=["BTCUSDT"])

        # Fake clock that returns same timestamp (simulates duplicate)
        ts = [1000000]

        def fake_clock() -> float:
            return ts[0] / 1000.0  # Same timestamp for both

        connector = BinanceWsConnector(config, transport=transport, clock=fake_clock)

        await connector.connect()

        snapshots: list[Snapshot] = []

        # Use asyncio.wait_for with a short timeout since we expect
        # only 1 snapshot (second is filtered by idempotency)
        async def collect_snapshots() -> None:
            async for snapshot in connector.iter_snapshots():
                snapshots.append(snapshot)

        # Expected to timeout - we stop collecting after timeout
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collect_snapshots(), timeout=0.5)

        await connector.close()

        # Only first snapshot should be yielded due to idempotency
        assert len(snapshots) == 1


# --- LiveFeed tests ---


class TestLiveFeed:
    """Tests for LiveFeed pipeline."""

    def test_process_snapshot_returns_features(self, sample_snapshot: Snapshot) -> None:
        """LiveFeed.process_snapshot_sync returns LiveFeaturesUpdate."""
        config = LiveFeedConfig(symbols=["BTCUSDT"])
        feed = LiveFeed(config)

        update = feed.process_snapshot_sync(sample_snapshot)

        assert update is not None
        assert isinstance(update, LiveFeaturesUpdate)
        assert update.symbol == "BTCUSDT"
        assert update.ts == sample_snapshot.ts
        assert update.features is not None

    def test_process_multiple_snapshots_tracks_bars(self) -> None:
        """LiveFeed tracks bar completion across multiple snapshots."""
        # Use 1-second bars for easier testing
        feature_config = FeatureEngineConfig(bar_interval_ms=1000)
        config = LiveFeedConfig(symbols=["BTCUSDT"], feature_config=feature_config)
        feed = LiveFeed(config)

        updates: list[LiveFeaturesUpdate] = []

        # Snapshots across bar boundary (1 second apart)
        for i in range(5):
            snapshot = Snapshot(
                ts=1000000 + i * 500,  # 500ms apart
                symbol="BTCUSDT",
                bid_price=Decimal("50000") + i,
                ask_price=Decimal("50001") + i,
                bid_qty=Decimal("1.0"),
                ask_qty=Decimal("1.0"),
                last_price=Decimal("50000.5") + i,
                last_qty=Decimal("0.5"),
            )
            update = feed.process_snapshot_sync(snapshot)
            if update:
                updates.append(update)

        assert len(updates) == 5
        # Check bars_available increases
        assert updates[-1].bars_available >= updates[0].bars_available

    def test_symbol_filtering(self) -> None:
        """LiveFeed filters by configured symbols."""
        config = LiveFeedConfig(symbols=["BTCUSDT"])  # Only BTCUSDT
        feed = LiveFeed(config)

        # BTCUSDT should be processed
        btc_snapshot = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        btc_update = feed.process_snapshot_sync(btc_snapshot)
        assert btc_update is not None

        # ETHUSDT should be filtered (config checks symbol before processing)
        assert not config.is_symbol_allowed("ETHUSDT")

    def test_warmup_detection(self) -> None:
        """LiveFeed correctly detects warmup state."""
        # Warmup requires 15 bars
        feature_config = FeatureEngineConfig(bar_interval_ms=100)
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            feature_config=feature_config,
            warmup_bars=3,  # Lower for testing
        )
        feed = LiveFeed(config)

        updates: list[LiveFeaturesUpdate] = []

        # Generate enough snapshots to complete 5 bars
        for i in range(50):
            snapshot = Snapshot(
                ts=1000000 + i * 50,  # 50ms apart, 2 per bar
                symbol="BTCUSDT",
                bid_price=Decimal("50000") + i,
                ask_price=Decimal("50001") + i,
                bid_qty=Decimal("1.0"),
                ask_qty=Decimal("1.0"),
                last_price=Decimal("50000.5") + i,
                last_qty=Decimal("0.5"),
            )
            update = feed.process_snapshot_sync(snapshot)
            if update:
                updates.append(update)

        # Eventually should be warmed up
        warmed_up = [u for u in updates if u.is_warmed_up]
        assert len(warmed_up) > 0

    def test_stats_tracking(self, sample_snapshot: Snapshot) -> None:
        """LiveFeed tracks statistics."""
        config = LiveFeedConfig()
        feed = LiveFeed(config)

        assert feed.stats.ticks_processed == 0

        feed.process_snapshot_sync(sample_snapshot)

        assert feed.stats.ticks_processed == 1
        assert feed.stats.ticks_received == 1

    def test_reset_clears_state(self, sample_snapshot: Snapshot) -> None:
        """LiveFeed.reset() clears all state."""
        config = LiveFeedConfig()
        feed = LiveFeed(config)

        feed.process_snapshot_sync(sample_snapshot)
        assert feed.stats.ticks_processed > 0

        feed.reset()

        assert feed.stats.ticks_processed == 0
        assert feed.feature_engine.get_bar_count("BTCUSDT") == 0


# --- Determinism tests ---


class TestDeterminism:
    """Tests for deterministic output."""

    def test_same_input_same_output(self) -> None:
        """Same input snapshots produce same feature output."""
        config = LiveFeedConfig(symbols=["BTCUSDT"])

        # Run 1
        feed1 = LiveFeed(config)
        snapshots = [
            Snapshot(
                ts=1000000 + i * 100,
                symbol="BTCUSDT",
                bid_price=Decimal("50000") + i,
                ask_price=Decimal("50001") + i,
                bid_qty=Decimal("1.0"),
                ask_qty=Decimal("1.0"),
                last_price=Decimal("50000.5") + i,
                last_qty=Decimal("0.5"),
            )
            for i in range(10)
        ]
        updates1 = [feed1.process_snapshot_sync(s) for s in snapshots]

        # Run 2
        feed2 = LiveFeed(config)
        updates2 = [feed2.process_snapshot_sync(s) for s in snapshots]

        # Compare features
        for u1, u2 in zip(updates1, updates2, strict=True):
            assert u1 is not None and u2 is not None
            assert u1.features.mid_price == u2.features.mid_price
            assert u1.features.spread_bps == u2.features.spread_bps
            assert u1.features.imbalance_l1_bps == u2.features.imbalance_l1_bps
            assert u1.bars_available == u2.bars_available


class TestLiveFeaturesUpdate:
    """Tests for LiveFeaturesUpdate dataclass."""

    def test_to_dict(self, sample_snapshot: Snapshot) -> None:
        """LiveFeaturesUpdate.to_dict() serializes correctly."""
        config = LiveFeedConfig()
        feed = LiveFeed(config)
        update = feed.process_snapshot_sync(sample_snapshot)

        assert update is not None
        d = update.to_dict()

        assert d["ts"] == sample_snapshot.ts
        assert d["symbol"] == "BTCUSDT"
        assert "features" in d
        assert isinstance(d["features"], dict)


# --- Golden output tests ---


class TestGoldenOutput:
    """Tests for deterministic output from fixture files."""

    def test_golden_bookticker_fixture(self) -> None:
        """Process fixture and verify deterministic features."""
        fixture_path = Path(__file__).parent.parent / "fixtures" / "ws" / "bookticker_btcusdt.json"
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

        # Read fixture (JSON array)
        messages = json.loads(fixture_path.read_text())
        assert len(messages) == 10

        def process_fixture_run(run_id: int) -> tuple[list[dict[str, Any]], str]:
            """Process fixture and return updates + digest."""
            # Fake clock for deterministic timestamps and latency
            ts_counter = [1000000]

            def fake_clock() -> float:
                ts_counter[0] += 100
                return ts_counter[0] / 1000.0

            # Create feed with deterministic clock
            config = LiveFeedConfig(symbols=["BTCUSDT"])
            feed = LiveFeed(config, clock=fake_clock)

            # Process each message (messages are already dicts from JSON array)
            updates: list[dict[str, Any]] = []
            for i, data in enumerate(messages):
                # Use deterministic timestamp for snapshot
                snapshot_ts = 1000100 + i * 100
                snapshot = Snapshot(
                    ts=snapshot_ts,
                    symbol=data["s"],
                    bid_price=Decimal(data["b"]),
                    ask_price=Decimal(data["a"]),
                    bid_qty=Decimal(data["B"]),
                    ask_qty=Decimal(data["A"]),
                    last_price=(Decimal(data["b"]) + Decimal(data["a"])) / 2,
                    last_qty=Decimal("0"),
                )
                update = feed.process_snapshot_sync(snapshot)
                if update:
                    # Exclude latency_ms from determinism check (timing-dependent)
                    d = update.to_dict()
                    d["latency_ms"] = 0  # Normalize
                    updates.append(d)

            serialized = json.dumps(updates, sort_keys=True, default=str)
            digest = hashlib.sha256(serialized.encode()).hexdigest()[:16]
            return updates, digest

        # Run 1
        updates1, digest1 = process_fixture_run(1)

        # Verify we got all updates
        assert len(updates1) == 10

        # Verify first and last features
        first = updates1[0]
        assert first["symbol"] == "BTCUSDT"
        assert first["features"]["mid_price"] == "50000.50"  # (50000 + 50001) / 2
        # spread_bps = int(1.0 / 50000.5 * 10000) = 0 (rounds down from 0.2)
        assert first["features"]["spread_bps"] == 0

        last = updates1[-1]
        assert last["symbol"] == "BTCUSDT"
        assert last["features"]["mid_price"] == "50009.50"

        # Run 2 - verify determinism
        _updates2, digest2 = process_fixture_run(2)

        assert digest1 == digest2, f"Determinism failed: {digest1} != {digest2}"
