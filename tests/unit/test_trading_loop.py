"""Unit tests for trading loop entrypoint (PR-P2-LOOP-0).

Tests cover:
- Engine initialization gauge in read_only mode
- validate_env() safety gates (ACK required for paper/live_trade)
- Full loop integration with FakeWsTransport fixture
"""

from __future__ import annotations

import asyncio
import json
import time as time_module

import pytest
from scripts.run_trading import build_connector, validate_env

from grinder.connectors.binance_ws import BINANCE_WS_MAINNET, FakeWsTransport
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.execution.port import NoOpExchangePort
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.paper.engine import PaperEngine


class FakeSleep:
    """Fake sleep for bounded-time testing."""

    def __init__(self) -> None:
        self.total_slept: float = 0.0
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.total_slept += seconds
        self.calls.append(seconds)


class TestEngineGauge:
    """Test that LiveEngineV0 sets grinder_live_engine_initialized=1."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def test_read_only_creates_engine_gauge_one(self) -> None:
        """Engine in read_only mode sets initialized gauge to 1."""
        paper = PaperEngine()
        port = NoOpExchangePort()
        config = LiveEngineConfig(mode=SafeMode.READ_ONLY)
        LiveEngineV0(paper_engine=paper, exchange_port=port, config=config)
        assert get_sor_metrics().engine_initialized is True


class TestValidateEnv:
    """Test validate_env() safety gates."""

    def test_paper_without_ack_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paper mode without ACK causes sys.exit(1)."""
        monkeypatch.setenv("GRINDER_TRADING_MODE", "paper")
        monkeypatch.delenv("GRINDER_TRADING_LOOP_ACK", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            validate_env()
        assert exc_info.value.code == 1

    def test_paper_with_ack_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paper mode with correct ACK returns SafeMode.PAPER."""
        monkeypatch.setenv("GRINDER_TRADING_MODE", "paper")
        monkeypatch.setenv("GRINDER_TRADING_LOOP_ACK", "YES_I_KNOW")
        mode = validate_env()
        assert mode == SafeMode.PAPER

    def test_read_only_no_ack_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default read_only mode works without any ACK."""
        monkeypatch.delenv("GRINDER_TRADING_MODE", raising=False)
        monkeypatch.delenv("GRINDER_TRADING_LOOP_ACK", raising=False)
        mode = validate_env()
        assert mode == SafeMode.READ_ONLY


class TestBuildConnector:
    """Test build_connector() network selection."""

    def test_default_uses_testnet(self) -> None:
        """Default build_connector uses testnet WS URL."""
        connector = build_connector(["BTCUSDT"], SafeMode.READ_ONLY, None)
        assert connector._config.use_testnet is True
        assert connector._config.ws_url == "wss://testnet.binance.vision/ws"

    def test_mainnet_flag_sets_mainnet_url(self) -> None:
        """build_connector(use_testnet=False) uses mainnet WS URL."""
        connector = build_connector(["BTCUSDT"], SafeMode.READ_ONLY, None, use_testnet=False)
        assert connector._config.use_testnet is False
        assert connector._config.ws_url == BINANCE_WS_MAINNET


class TestTradingLoop:
    """Test full trading loop integration with fixture data."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    @pytest.mark.asyncio
    async def test_loop_processes_fixture_snapshots(self) -> None:
        """Loop processes N snapshots from FakeWsTransport and sets gauge."""
        messages = [
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": "50000.00",
                    "B": "1.5",
                    "a": "50001.00",
                    "A": "2.0",
                }
            ),
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": "50002.00",
                    "B": "1.2",
                    "a": "50003.00",
                    "A": "1.8",
                }
            ),
            json.dumps(
                {
                    "s": "BTCUSDT",
                    "b": "50004.00",
                    "B": "1.0",
                    "a": "50005.00",
                    "A": "1.5",
                }
            ),
        ]
        transport = FakeWsTransport(messages=messages, delay_ms=2)
        connector = LiveConnectorV0(
            config=LiveConnectorConfig(symbols=["BTCUSDT"], ws_transport=transport),
            clock=time_module,
            sleep_func=FakeSleep(),
        )
        engine = LiveEngineV0(
            paper_engine=PaperEngine(),
            exchange_port=NoOpExchangePort(),
            config=LiveEngineConfig(mode=SafeMode.READ_ONLY),
        )

        await connector.connect()
        ticks = 0
        try:
            async with asyncio.timeout(5):
                async for snapshot in connector.iter_snapshots():
                    engine.process_snapshot(snapshot)
                    ticks += 1
                    if ticks >= 3:
                        break
        except TimeoutError:
            pass
        finally:
            await connector.close()

        assert ticks == 3
        assert get_sor_metrics().engine_initialized is True
