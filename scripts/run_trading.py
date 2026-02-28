#!/usr/bin/env python3
"""Run GRINDER trading loop.

Usage:
    python -m scripts.run_trading --symbols BTCUSDT,ETHUSDT --metrics-port 9090 [--mainnet]

Rehearsal knobs (safe with NoOpExchangePort):
    --armed                 Arm engine gates (lets actions reach fill-prob gate)
    --paper-size-per-level  Override PaperEngine size_per_level (Decimal, e.g. 0.001)

Exchange port selection:
    --exchange-port noop        Default, no real orders (NoOpExchangePort)
    --exchange-port futures     BinanceFuturesPort (USDT-M). Requires 5 safety gates:
                                1. mode=live_trade  2. --armed  3. ALLOW_MAINNET_TRADE=1
                                4. GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET
                                5. BINANCE_API_KEY + BINANCE_API_SECRET set
    --max-notional-per-order    Max notional per order in USD (default 100, rehearsal cap)

HA mode (GRINDER_HA_ENABLED=true):
    - Starts LeaderElector for single-active coordination
    - /readyz returns 200 only when loop_ready AND role==ACTIVE
    - Snapshot processing skipped when not ACTIVE (fail-closed)
    - Elector failure → role stays UNKNOWN → /readyz=503

Env vars:
    GRINDER_TRADING_MODE        read_only (default) | paper | live_trade
    GRINDER_TRADING_LOOP_ACK    Must be YES_I_KNOW for paper/live_trade
    GRINDER_FILL_MODEL_DIR      Path to fill model directory (enables fill-prob gate)
    ALLOW_MAINNET_TRADE         Existing guard (enforced by connector for live_trade)
    GRINDER_REAL_PORT_ACK       Must be YES_I_REALLY_WANT_MAINNET for --exchange-port futures
    GRINDER_HA_ENABLED          true|1|yes to enable HA leader election
    BINANCE_API_KEY             Required for --exchange-port futures
    BINANCE_API_SECRET          Required for --exchange-port futures

Safety:
    - Default mode is read_only (no write ops).
    - paper / live_trade require explicit ACK env.
    - Default exchange port is NoOp — no real orders placed.
    - --exchange-port futures requires ALL 5 safety gates to pass.
    - --armed only affects the gate chain inside LiveEngineV0._process_action().
      With NoOpExchangePort, arming has zero real-world effect.

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
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from grinder.connectors.binance_ws import BINANCE_WS_MAINNET, FakeWsTransport
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.constraint_provider import (
    ConstraintProvider,
    ConstraintProviderConfig,
)
from grinder.execution.port import ExchangePort, NoOpExchangePort
from grinder.execution.port_metrics import get_port_metrics
from grinder.gating.metrics import get_gating_metrics
from grinder.ha.leader import LeaderElector, LeaderElectorConfig
from grinder.ha.role import HARole, get_ha_state
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.ml.fill_model_loader import load_fill_model_v0
from grinder.net.fixture_guard import install_fixture_network_guard
from grinder.observability import (
    build_healthz_body,
    build_metrics_body,
    set_ready_fn,
    set_start_time,
)
from grinder.paper.engine import PaperEngine
from scripts.http_measured_client import RequestsHttpClient, build_measured_client

if TYPE_CHECKING:
    from grinder.execution.engine import SymbolConstraints

# Module-level readiness flags.
_loop_ready = False
_ha_enabled = False


def is_trading_ready() -> bool:
    """Check if trading loop is ready.

    Ready requires loop_ready=True AND (HA disabled OR role==ACTIVE).
    """
    if not _loop_ready:
        return False
    if _ha_enabled:
        return get_ha_state().role == HARole.ACTIVE
    return True


def reset_trading_state() -> None:
    """Reset module-level trading state (for test cleanup)."""
    global _loop_ready, _ha_enabled  # noqa: PLW0603
    _loop_ready = False
    _ha_enabled = False


class TradingHealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and metrics.

    Endpoints:
        /healthz - Always 200 if process alive (liveness)
        /readyz  - 200 if loop_ready AND (HA disabled OR ACTIVE), 503 otherwise
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
        """Send readiness check (200 if ready, 503 otherwise)."""
        ready = is_trading_ready()
        body = json.dumps(
            {
                "ready": ready,
                "loop_ready": _loop_ready,
                "ha_enabled": _ha_enabled,
                "ha_role": get_ha_state().role.value if _ha_enabled else "n/a",
                "mode": "trading_loop",
            }
        )
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


def is_ha_enabled() -> bool:
    """Check if HA mode is enabled via environment."""
    return os.environ.get("GRINDER_HA_ENABLED", "").lower() in ("true", "1", "yes")


def start_ha_elector() -> LeaderElector | None:
    """Start HA leader election if enabled.

    Fail-closed: if elector fails to start, role stays UNKNOWN → /readyz=503.

    Returns:
        LeaderElector instance if started, None otherwise.
    """
    if not is_ha_enabled():
        print("  HA mode: DISABLED (set GRINDER_HA_ENABLED=true to enable)")
        return None

    print("  HA mode: ENABLED")
    try:
        config = LeaderElectorConfig()
        print(f"    Redis URL: {config.redis_url}")
        print(f"    Lock TTL: {config.lock_ttl_ms}ms")
        print(f"    Instance ID: {config.instance_id}")
        elector = LeaderElector(config)
        elector.start()
        print("    LeaderElector started")
        return elector
    except Exception as e:
        print(f"    WARNING: Failed to start LeaderElector: {e}")
        print("    Running without HA (role stays UNKNOWN → /readyz=503)")
        return None


def validate_real_port_gates(mode: SafeMode, armed: bool) -> None:
    """Validate all 5 safety gates for real exchange port.

    Gates:
        1. mode == LIVE_TRADE
        2. armed == True
        3. ALLOW_MAINNET_TRADE=1
        4. GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET
        5. BINANCE_API_KEY + BINANCE_API_SECRET set (checked in build_exchange_port)

    Raises:
        SystemExit: If any gate fails.
    """
    if mode != SafeMode.LIVE_TRADE:
        print(f"ERROR: --exchange-port futures requires mode=live_trade (got {mode.value})")
        sys.exit(1)

    if not armed:
        print("ERROR: --exchange-port futures requires --armed")
        sys.exit(1)

    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in ("1", "true", "yes")
    if not allow_mainnet:
        print("ERROR: --exchange-port futures requires ALLOW_MAINNET_TRADE=1")
        sys.exit(1)

    real_ack = os.environ.get("GRINDER_REAL_PORT_ACK", "")
    if real_ack != "YES_I_REALLY_WANT_MAINNET":
        print(
            "ERROR: --exchange-port futures requires GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET"
        )
        sys.exit(1)


def build_exchange_port(
    port_name: str,
    mode: SafeMode,
    armed: bool,
    symbols: list[str],
    max_notional: Decimal,
) -> ExchangePort:
    """Build exchange port by name.

    Args:
        port_name: "noop" or "futures".
        mode: SafeMode for config.
        armed: Whether engine is armed.
        symbols: Trading symbols (used as whitelist for futures).
        max_notional: Max notional per order (for futures config).

    Returns:
        ExchangePort instance.

    Raises:
        SystemExit: If gates fail or API keys missing for futures.
    """
    if port_name == "noop":
        return NoOpExchangePort()

    if port_name == "futures":
        validate_real_port_gates(mode, armed)

        api_key = os.environ.get("BINANCE_API_KEY", "").strip()
        api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            print("ERROR: --exchange-port futures requires BINANCE_API_KEY and BINANCE_API_SECRET")
            sys.exit(1)

        inner = RequestsHttpClient(port_name="futures")
        http_client = build_measured_client(inner)

        config = BinanceFuturesPortConfig(
            mode=mode,
            base_url=BINANCE_FUTURES_MAINNET_URL,
            api_key=api_key,
            api_secret=api_secret,
            symbol_whitelist=symbols,
            allow_mainnet=True,
            max_notional_per_order=max_notional,
        )
        return BinanceFuturesPort(http_client=http_client, config=config)  # type: ignore[return-value]

    print(f"ERROR: Unknown exchange port: {port_name!r}. Must be: noop, futures")
    sys.exit(1)


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


def _load_symbol_constraints() -> dict[str, SymbolConstraints] | None:
    """Load symbol constraints from exchange info (fail-open).

    Tries local cache first, then Binance Futures API.
    Returns None if both fail (constraints will be skipped).
    """
    provider = ConstraintProvider(
        config=ConstraintProviderConfig(allow_fetch=False),
    )
    constraints = provider.get_constraints()
    if constraints:
        return constraints

    # Try API fetch (requires network)
    try:
        http = RequestsHttpClient(port_name="constraint_fetch")
        api_provider = ConstraintProvider(
            http_client=http,
            config=ConstraintProviderConfig(allow_fetch=True),
        )
        constraints = api_provider.get_constraints()
        if constraints:
            return constraints
    except Exception as e:
        print(f"  Constraint fetch failed (fail-open): {e}")

    return None


def build_engine(
    mode: SafeMode,
    *,
    armed: bool = False,
    paper_size_per_level: Decimal | None = None,
    exchange_port: ExchangePort | None = None,
) -> LiveEngineV0:
    """Build LiveEngineV0 with configurable ExchangePort.

    If exchange_port is None, defaults to NoOpExchangePort (no real orders).

    If GRINDER_FILL_MODEL_DIR is set, loads FillModelV0 for fill probability
    gating (fail-open: load error -> None -> gate skipped).

    Loads symbol constraints (tick_size, step_size) from exchange info
    for price/qty rounding (fail-open: if unavailable, uses price_precision only).

    Args:
        mode: SafeMode for engine config.
        armed: Arm engine gate chain (lets actions flow to fill-prob gate).
            Safe with NoOpExchangePort — zero real-world effect.
        paper_size_per_level: Override PaperEngine size_per_level.
            Default PaperEngine uses 100 (base asset units), which exceeds
            notional gating limits at current BTC prices. Use e.g. 0.001
            for rehearsal to get actions through gating.
        exchange_port: ExchangePort to use. Defaults to NoOpExchangePort.

    Returns:
        Configured LiveEngineV0 instance (gauge set to 1 after init).
    """
    # Load symbol constraints for tick_size rounding (fail-open)
    symbol_constraints = _load_symbol_constraints()
    constraints_enabled = symbol_constraints is not None
    if constraints_enabled and symbol_constraints is not None:
        print(f"  Symbol constraints loaded: {len(symbol_constraints)} symbols")
    else:
        print("  Symbol constraints not available (fail-open, using price_precision only)")

    if paper_size_per_level is not None:
        paper_engine = PaperEngine(
            size_per_level=paper_size_per_level,
            constraints_enabled=constraints_enabled,
            symbol_constraints=symbol_constraints,
        )
    else:
        paper_engine = PaperEngine(
            constraints_enabled=constraints_enabled,
            symbol_constraints=symbol_constraints,
        )
    port = exchange_port if exchange_port is not None else NoOpExchangePort()
    config = LiveEngineConfig(armed=armed, mode=mode)

    fill_model = None
    model_dir = os.environ.get("GRINDER_FILL_MODEL_DIR", "").strip()
    if model_dir:
        fill_model = load_fill_model_v0(model_dir)
        if fill_model is not None:
            print(
                f"  Fill model loaded: {len(fill_model.bins)} bins, prior={fill_model.global_prior_bps} bps"
            )
        else:
            print("  Fill model load FAILED (fail-open, gate skipped)")

    return LiveEngineV0(
        paper_engine=paper_engine,
        exchange_port=port,
        config=config,
        fill_model=fill_model,
    )


async def trading_loop(
    connector: LiveConnectorV0,
    engine: LiveEngineV0,
    shutdown: asyncio.Event,
    duration_s: int,
) -> None:
    """Run the trading loop: connector -> engine.process_snapshot().

    Sets module-level _loop_ready flag after connector.connect() succeeds.
    Resets _loop_ready in finally block.

    When HA is enabled, skips snapshot processing if role != ACTIVE (fail-closed).

    Args:
        connector: Connected LiveConnectorV0.
        engine: Initialized LiveEngineV0.
        shutdown: Event to signal graceful stop.
        duration_s: Max duration (0 = infinite).
    """
    global _loop_ready  # noqa: PLW0603
    await connector.connect()
    _loop_ready = True
    print("  /readyz now returning 200 (if HA permits)")
    start = time.time()
    tick_count = 0
    ha_skip_count = 0
    try:
        async for snapshot in connector.iter_snapshots():
            if shutdown.is_set():
                break
            if duration_s > 0 and (time.time() - start) >= duration_s:
                print(f"\nDuration ({duration_s}s) reached after {tick_count} ticks.")
                break
            # HA gating: skip processing when not ACTIVE
            if _ha_enabled and get_ha_state().role != HARole.ACTIVE:
                ha_skip_count += 1
                if ha_skip_count % 100 == 1:
                    print(f"  HA: not ACTIVE, skipping snapshot (total skipped: {ha_skip_count})")
                continue
            engine.process_snapshot(snapshot)
            tick_count += 1
            if tick_count % 100 == 0:
                print(f"  Processed {tick_count} ticks ({snapshot.symbol})")
    finally:
        _loop_ready = False
        await connector.close()
        print(f"  Trading loop stopped. Total ticks: {tick_count}, HA skips: {ha_skip_count}")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
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
    parser.add_argument(
        "--armed",
        action="store_true",
        default=False,
        help="Arm engine gate chain (lets actions reach fill-prob gate). Safe with NoOpExchangePort.",
    )
    parser.add_argument(
        "--paper-size-per-level",
        type=str,
        default=None,
        help="Override PaperEngine size_per_level (Decimal, e.g. 0.001). "
        "Default 100 exceeds notional limits at current BTC prices.",
    )
    parser.add_argument(
        "--exchange-port",
        type=str,
        default="noop",
        choices=["noop", "futures"],
        help="Exchange port: noop (default, no orders) or futures (BinanceFuturesPort, 5 gates).",
    )
    parser.add_argument(
        "--max-notional-per-order",
        type=str,
        default="100",
        help="Max notional per order in USD (default 100, rehearsal cap). Used with --exchange-port futures.",
    )
    return parser


async def _drain_pending_tasks() -> None:
    """Cancel and await all pending tasks (safety net for clean shutdown)."""
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:  # noqa: PLR0915
    global _ha_enabled  # noqa: PLW0603

    args = build_parser().parse_args()

    # Fixture network airgap (PR-NETLOCK-1) — must be before ANY network-touching code
    if args.fixture:
        install_fixture_network_guard()

    mode = validate_env()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    use_testnet = not args.mainnet
    max_notional = Decimal(args.max_notional_per_order)

    paper_size: Decimal | None = None
    if args.paper_size_per_level is not None:
        paper_size = Decimal(args.paper_size_per_level)

    # HA lifecycle
    _ha_enabled = is_ha_enabled()
    elector = start_ha_elector()

    # Exchange port
    port = build_exchange_port(args.exchange_port, mode, args.armed, symbols, max_notional)

    # Boot summary
    print(
        f"GRINDER TRADING LOOP | mode={mode.value} symbols={symbols} "
        f"port={args.exchange_port} armed={args.armed} "
        f"ha={_ha_enabled} net={'mainnet' if args.mainnet else 'testnet'} "
        f"max_notional={max_notional}"
    )
    if paper_size is not None:
        print(f"  Paper size_per_level: {paper_size}")
    if args.fixture:
        print(f"  Fixture: {args.fixture}")
        print("  Network guard: ACTIVE (external connections blocked)")

    server = run_server(args.metrics_port)
    print(f"  Health endpoint: http://localhost:{args.metrics_port}/healthz")

    engine = build_engine(
        mode,
        armed=args.armed,
        paper_size_per_level=paper_size,
        exchange_port=port,
    )
    print("  Engine initialized: grinder_live_engine_initialized=1")

    # Pre-populate zero-value gating metrics for Prometheus visibility
    get_gating_metrics().initialize_zero_series()

    # Pre-populate zero-value port order attempt metrics (PR-FUT-1)
    get_port_metrics().initialize_zero_series(args.exchange_port)

    # Register readyz callback so /metrics emits grinder_readyz_ready gauge (PR-ALERTS-0)
    set_ready_fn(is_trading_ready)

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
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(_drain_pending_tasks())
        loop.close()
        if elector is not None:
            print("  Stopping LeaderElector...")
            elector.stop()
        server.shutdown()
        print("GRINDER TRADING LOOP stopped.")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
