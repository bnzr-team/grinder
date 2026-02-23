#!/usr/bin/env python3
"""Run GRINDER trading loop.

Usage:
    python -m scripts.run_trading --symbols BTCUSDT,ETHUSDT --metrics-port 9090 [--mainnet]

Env vars:
    GRINDER_TRADING_MODE        read_only (default) | paper | live_trade
    GRINDER_TRADING_LOOP_ACK    Must be YES_I_KNOW for paper/live_trade
    ALLOW_MAINNET_TRADE         Existing guard (enforced by connector for live_trade)

Safety:
    - Default mode is read_only (no write ops).
    - paper / live_trade require explicit ACK env.
    - ExchangePort is NoOp — no real orders are placed regardless of mode.
      Modes affect gating/guards only.

Fixture mode (--fixture):
    Pass a JSONL file (one bookTicker JSON object per line) to run
    with canned data instead of a real WebSocket connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from grinder.connectors.binance_ws import BINANCE_WS_MAINNET, FakeWsTransport
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.execution.port import NoOpExchangePort
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.observability import (
    build_healthz_body,
    build_metrics_body,
    set_start_time,
)
from grinder.paper.engine import PaperEngine

# Module-level readiness flag: True when engine created AND connector connected.
_ready = False


def is_trading_ready() -> bool:
    """Check if trading loop is ready (engine + connector)."""
    return _ready


class TradingHealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and metrics.

    Endpoints:
        /healthz - Always 200 if process alive (liveness)
        /readyz  - 200 if engine+connector ready, 503 otherwise
        /metrics - Prometheus metrics
    """

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/healthz":
            self._send_health()
        elif self.path == "/readyz":
            self._send_ready()
        elif self.path == "/metrics":
            self._send_metrics()
        else:
            self.send_error(404)

    def _send_health(self) -> None:
        """Send health check response (always 200 if alive)."""
        body = build_healthz_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_ready(self) -> None:
        """Send readiness check (200 if engine created + connector connected)."""
        ready = is_trading_ready()
        body = json.dumps({"ready": ready, "mode": "trading_loop"})
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_metrics(self) -> None:
        """Send Prometheus metrics."""
        body = build_metrics_body()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default logging."""
        pass


def run_server(port: int) -> HTTPServer:
    """Start HTTP server in background thread."""
    set_start_time(time.time())
    server = HTTPServer(("0.0.0.0", port), TradingHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def validate_env() -> SafeMode:
    """Validate trading mode and ACK env vars.

    Returns:
        SafeMode enum value.

    Raises:
        SystemExit: If mode is invalid or ACK is missing for paper/live_trade.
    """
    mode_str = os.environ.get("GRINDER_TRADING_MODE", "read_only").lower()
    try:
        mode = SafeMode(mode_str)
    except ValueError:
        print(
            f"ERROR: GRINDER_TRADING_MODE={mode_str!r} invalid. "
            "Must be: read_only, paper, live_trade"
        )
        sys.exit(1)

    if mode in (SafeMode.PAPER, SafeMode.LIVE_TRADE):
        ack = os.environ.get("GRINDER_TRADING_LOOP_ACK", "")
        if ack != "YES_I_KNOW":
            print(f"ERROR: GRINDER_TRADING_LOOP_ACK must be YES_I_KNOW for mode={mode.value}")
            sys.exit(1)

    if mode == SafeMode.LIVE_TRADE:
        print("  WARNING: live_trade mode requires ALLOW_MAINNET_TRADE=1 (enforced by connector)")

    return mode


def build_connector(
    symbols: list[str],
    mode: SafeMode,
    fixture_path: str | None,
    *,
    use_testnet: bool = True,
) -> LiveConnectorV0:
    """Build LiveConnectorV0 with optional fixture transport.

    Args:
        symbols: List of trading symbols.
        mode: SafeMode for the connector.
        fixture_path: Optional path to JSONL fixture file.
        use_testnet: Use testnet WS endpoint (default True for safety).

    Returns:
        Configured LiveConnectorV0 instance.
    """
    ws_transport = None
    if fixture_path:
        with Path(fixture_path).open() as f:
            messages = [line.strip() for line in f if line.strip()]
        ws_transport = FakeWsTransport(messages=messages, delay_ms=100)

    ws_url = BINANCE_WS_MAINNET if not use_testnet else "wss://testnet.binance.vision/ws"

    config = LiveConnectorConfig(
        mode=mode,
        symbols=symbols,
        ws_transport=ws_transport,
        ws_url=ws_url,
        use_testnet=use_testnet,
    )
    return LiveConnectorV0(config=config)


def build_engine(mode: SafeMode) -> LiveEngineV0:
    """Build LiveEngineV0 with NoOpExchangePort.

    NoOpExchangePort means no real orders are placed regardless of mode.
    Modes affect gating/guards only.

    Args:
        mode: SafeMode for engine config.

    Returns:
        Configured LiveEngineV0 instance (gauge set to 1 after init).
    """
    paper_engine = PaperEngine()
    port = NoOpExchangePort()
    config = LiveEngineConfig(mode=mode)  # armed=False by default
    return LiveEngineV0(
        paper_engine=paper_engine,
        exchange_port=port,
        config=config,
    )


async def trading_loop(
    connector: LiveConnectorV0,
    engine: LiveEngineV0,
    shutdown: asyncio.Event,
    duration_s: int,
) -> None:
    """Run the trading loop: connector → engine.process_snapshot().

    Sets module-level _ready flag after connector.connect() succeeds.
    Resets _ready in finally block.

    Args:
        connector: Connected LiveConnectorV0.
        engine: Initialized LiveEngineV0.
        shutdown: Event to signal graceful stop.
        duration_s: Max duration (0 = infinite).
    """
    global _ready  # noqa: PLW0603
    await connector.connect()
    _ready = True
    print("  /readyz now returning 200")
    start = time.time()
    tick_count = 0
    try:
        async for snapshot in connector.iter_snapshots():
            if shutdown.is_set():
                break
            if duration_s > 0 and (time.time() - start) >= duration_s:
                print(f"\nDuration ({duration_s}s) reached after {tick_count} ticks.")
                break
            engine.process_snapshot(snapshot)
            tick_count += 1
            if tick_count % 100 == 0:
                print(f"  Processed {tick_count} ticks ({snapshot.symbol})")
    finally:
        _ready = False
        await connector.close()
        print(f"  Trading loop stopped. Total ticks: {tick_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GRINDER trading loop")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT")
    parser.add_argument("--duration-s", type=int, default=0)
    parser.add_argument("--metrics-port", type=int, default=9090)
    parser.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="Path to JSONL fixture (one bookTicker JSON per line)",
    )
    parser.add_argument(
        "--mainnet",
        action="store_true",
        default=False,
        help="Use mainnet WS endpoint instead of testnet (safe for read_only)",
    )
    args = parser.parse_args()

    mode = validate_env()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    use_testnet = not args.mainnet

    print("GRINDER TRADING LOOP starting...")
    print(f"  Mode: {mode.value}")
    print(f"  Symbols: {symbols}")
    print(f"  Metrics port: {args.metrics_port}")
    print(f"  Network: {'mainnet' if args.mainnet else 'testnet'}")
    if args.fixture:
        print(f"  Fixture: {args.fixture}")

    server = run_server(args.metrics_port)
    print(f"  Health endpoint: http://localhost:{args.metrics_port}/healthz")

    engine = build_engine(mode)
    print("  Engine initialized: grinder_live_engine_initialized=1")

    connector = build_connector(symbols, mode, args.fixture, use_testnet=use_testnet)

    # Async loop with signal handling
    loop = asyncio.new_event_loop()
    shutdown = asyncio.Event()

    def handle_signal(*_: object) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("\nGRINDER TRADING LOOP running. Press Ctrl+C to stop.")
    exit_code = 0
    try:
        loop.run_until_complete(trading_loop(connector, engine, shutdown, args.duration_s))
    except Exception as exc:
        print(f"GRINDER TRADING LOOP FATAL: {exc}")
        exit_code = 2
    finally:
        loop.close()
        server.shutdown()
        print("GRINDER TRADING LOOP stopped.")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
