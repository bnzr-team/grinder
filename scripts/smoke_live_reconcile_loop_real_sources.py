#!/usr/bin/env python3
"""LC-14b: Smoke test for ReconcileLoop with real sources (detect-only).

This script verifies the ReconcileLoop can:
1. Connect to real Binance Futures sources (WS user-data, REST snapshots)
2. Fetch real price data
3. Run in detect-only mode (0 execution side-effects)

SAFETY:
- Uses FakePort (no real order execution)
- detect_only=True enforced at ReconcileLoopConfig level
- Even with real WS/REST connections, NO trading actions occur

Usage:
    # Dry-run (no API keys needed - will fail gracefully)
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop_real_sources

    # With testnet API keys
    BINANCE_TESTNET_API_KEY=xxx BINANCE_TESTNET_SECRET=xxx \
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop_real_sources --testnet

    # With mainnet API keys (read-only, still detect-only)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=xxx \
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop_real_sources

See ADR-049 for design decisions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import requests

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.execution.binance_port import HttpResponse

# Configure logging first
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# =============================================================================
# Requests-based HTTP Client
# =============================================================================


@dataclass
class RequestsHttpClient:
    """HTTP client using requests library for real API calls."""

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",  # noqa: ARG002
    ) -> HttpResponse:
        """Execute HTTP request via requests library."""
        timeout_s = timeout_ms / 1000.0

        try:
            if method == "GET":
                resp = requests.get(url, params=params, headers=headers, timeout=timeout_s)
            elif method == "POST":
                resp = requests.post(url, params=params, headers=headers, timeout=timeout_s)
            elif method == "DELETE":
                resp = requests.delete(url, params=params, headers=headers, timeout=timeout_s)
            else:
                raise ConnectorNonRetryableError(f"Unsupported method: {method}")

            return HttpResponse(
                status_code=resp.status_code,
                json_data=resp.json() if resp.content else {},
            )

        except requests.exceptions.Timeout as e:
            raise ConnectorTransientError(f"Request timeout: {e}") from e
        except requests.exceptions.ConnectionError as e:
            raise ConnectorTransientError(f"Connection error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ConnectorNonRetryableError(f"Request error: {e}") from e


# =============================================================================
# FakePort for no-execution proof
# =============================================================================


@dataclass
class FakePort:
    """Fake port that records calls without executing.

    Used to prove detect-only mode: any call here means remediation was attempted.
    In detect-only mode, this should have 0 calls.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Record cancel attempt (should NOT be called in detect-only)."""
        call = {
            "action": "cancel_order",
            "symbol": symbol,
            "client_order_id": client_order_id,
            "ts": int(time.time() * 1000),
        }
        self.calls.append(call)
        logger.warning("FAKE_PORT_CANCEL", extra=call)
        return {"status": "CANCELED", "clientOrderId": client_order_id}

    def place_market_order(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        """Record market order attempt (should NOT be called in detect-only)."""
        call = {
            "action": "place_market_order",
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "ts": int(time.time() * 1000),
        }
        self.calls.append(call)
        logger.warning("FAKE_PORT_MARKET_ORDER", extra=call)
        return {"status": "FILLED", "symbol": symbol}


# =============================================================================
# Source Status Tracking
# =============================================================================


@dataclass
class SourceStatus:
    """Status of data sources."""

    ws_connected: bool = False
    ws_events_received: int = 0
    ws_error: str | None = None

    rest_snapshot_ok: bool = False
    rest_orders_count: int = 0
    rest_positions_count: int = 0
    rest_error: str | None = None

    price_getter_ok: bool = False
    price_fetched: Decimal | None = None
    price_error: str | None = None


# =============================================================================
# Main Smoke Test
# =============================================================================


def get_api_credentials(testnet: bool) -> tuple[str, str]:
    """Get API credentials from environment."""
    if testnet:
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.environ.get("BINANCE_TESTNET_SECRET", "")
    else:
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
    return api_key, api_secret


def test_rest_snapshot(
    api_key: str,
    api_secret: str,
    testnet: bool,
    status: SourceStatus,
) -> None:
    """Test REST snapshot client."""
    from grinder.reconcile.observed_state import ObservedStateStore  # noqa: PLC0415
    from grinder.reconcile.snapshot_client import (  # noqa: PLC0415
        SnapshotClient,
        SnapshotClientConfig,
    )

    if not api_key or not api_secret:
        status.rest_error = "No API credentials"
        logger.warning("REST: Skipping - no API credentials")
        return

    try:
        base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"

        config = SnapshotClientConfig(
            base_url=base_url,
            api_key=api_key,
            api_secret=api_secret,
            timeout_ms=10000,
        )

        http_client = RequestsHttpClient()
        observed = ObservedStateStore()

        client = SnapshotClient(
            http_client=http_client,
            config=config,
            observed=observed,
        )

        # Fetch open orders
        orders = client.fetch_open_orders()
        status.rest_orders_count = len(orders)

        # Fetch positions
        positions = client.fetch_positions()
        status.rest_positions_count = len(positions)

        status.rest_snapshot_ok = True
        logger.info(
            "REST: Snapshot OK",
            extra={
                "orders": status.rest_orders_count,
                "positions": status.rest_positions_count,
            },
        )

    except Exception as e:
        status.rest_error = str(e)
        logger.warning("REST: Failed", extra={"error": str(e)})


def test_price_getter(
    testnet: bool,
    status: SourceStatus,
) -> None:
    """Test price getter."""
    from grinder.reconcile.price_getter import create_price_getter  # noqa: PLC0415

    try:
        http_client = RequestsHttpClient()
        price_getter = create_price_getter(http_client, testnet=testnet)

        # Fetch price for BTCUSDT
        price = price_getter.get_price("BTCUSDT")
        if price is not None:
            status.price_getter_ok = True
            status.price_fetched = price
            logger.info("PRICE: Fetch OK", extra={"symbol": "BTCUSDT", "price": str(price)})
        else:
            status.price_error = "No price returned"
            logger.warning("PRICE: No price returned for BTCUSDT")

    except Exception as e:
        status.price_error = str(e)
        logger.warning("PRICE: Failed", extra={"error": str(e)})


async def test_ws_connection(
    api_key: str,
    api_secret: str,
    testnet: bool,
    status: SourceStatus,
    timeout_sec: float = 10.0,
) -> None:
    """Test WS user-data connection (brief)."""
    from grinder.connectors.binance_user_data_ws import (  # noqa: PLC0415
        FuturesUserDataWsConnector,
        ListenKeyConfig,
        ListenKeyManager,
        UserDataWsConfig,
    )

    if not api_key or not api_secret:
        status.ws_error = "No API credentials"
        logger.warning("WS: Skipping - no API credentials")
        return

    try:
        base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"

        config = UserDataWsConfig(
            base_url=base_url,
            api_key=api_key,
            use_testnet=testnet,
        )

        # ListenKeyManager needs ListenKeyConfig
        listen_key_config = ListenKeyConfig(
            base_url=base_url,
            api_key=api_key,
        )

        http_client = RequestsHttpClient()
        listen_key_manager = ListenKeyManager(http_client, listen_key_config)

        connector = FuturesUserDataWsConnector(
            config=config,
            listen_key_manager=listen_key_manager,
        )

        logger.info("WS: Attempting connection...")

        # Try to connect with timeout
        try:
            await asyncio.wait_for(connector.connect(), timeout=timeout_sec)
            status.ws_connected = True
            logger.info("WS: Connected OK")

            # Wait briefly for events (if any)
            deadline = time.time() + 2.0
            async for event in connector.iter_events():
                status.ws_events_received += 1
                logger.info("WS: Event received", extra={"event_type": type(event).__name__})
                if time.time() > deadline:
                    break

        except TimeoutError:
            status.ws_error = "Connection timeout"
            logger.warning("WS: Connection timeout")

        finally:
            await connector.close()
            logger.info("WS: Closed")

    except Exception as e:
        status.ws_error = str(e)
        logger.warning("WS: Failed", extra={"error": str(e)})


def run_reconcile_loop_smoke(
    duration_sec: int,
    interval_ms: int,
    fake_port: FakePort,
) -> dict[str, Any]:
    """Run ReconcileLoop with fake dependencies for detect-only proof."""
    from grinder.live.reconcile_loop import ReconcileLoop, ReconcileLoopConfig  # noqa: PLC0415
    from grinder.reconcile.config import ReconcileConfig, RemediationAction  # noqa: PLC0415
    from grinder.reconcile.engine import ReconcileEngine  # noqa: PLC0415
    from grinder.reconcile.expected_state import ExpectedStateStore  # noqa: PLC0415
    from grinder.reconcile.identity import OrderIdentityConfig  # noqa: PLC0415
    from grinder.reconcile.metrics import get_reconcile_metrics  # noqa: PLC0415
    from grinder.reconcile.observed_state import ObservedStateStore  # noqa: PLC0415
    from grinder.reconcile.remediation import RemediationExecutor  # noqa: PLC0415
    from grinder.reconcile.runner import ReconcileRunner  # noqa: PLC0415

    # Create stores
    expected = ExpectedStateStore()
    observed = ObservedStateStore()
    identity_config = OrderIdentityConfig(prefix="grinder_smoke", allowed_strategies={"1"})

    # Create engine
    reconcile_config = ReconcileConfig(
        action=RemediationAction.NONE,  # Detect-only
        dry_run=True,
        allow_active_remediation=False,
    )
    metrics = get_reconcile_metrics()
    engine = ReconcileEngine(
        config=reconcile_config,
        expected=expected,
        observed=observed,
        metrics=metrics,
        identity_config=identity_config,
    )

    # Create executor with FakePort
    executor = RemediationExecutor(
        config=reconcile_config,
        port=fake_port,  # type: ignore[arg-type]
        armed=False,
        symbol_whitelist=["BTCUSDT"],
        identity_config=identity_config,
    )

    # Create runner
    runner = ReconcileRunner(
        engine=engine,
        executor=executor,
        observed=observed,
        price_getter=lambda _: Decimal("50000.00"),  # Fake price
    )

    # Create loop with detect_only=True (LC-14b enforcer)
    loop_config = ReconcileLoopConfig(
        enabled=True,
        interval_ms=interval_ms,
        detect_only=True,  # Hard enforcer
        require_active_role=False,
    )
    loop = ReconcileLoop(runner=runner, config=loop_config)

    logger.info("Starting ReconcileLoop...")
    loop.start()

    # Run for duration
    time.sleep(duration_sec)

    logger.info("Stopping ReconcileLoop...")
    loop.stop()

    # Get stats
    stats = loop.stats
    return {
        "runs_total": stats.runs_total,
        "runs_with_mismatch": stats.runs_with_mismatch,
        "runs_skipped_role": stats.runs_skipped_role,
        "runs_with_error": stats.runs_with_error,
        "last_run_ts_ms": stats.last_run_ts_ms,
        "port_calls": len(fake_port.calls),
    }


def main() -> int:  # noqa: PLR0915
    """Run smoke test."""
    parser = argparse.ArgumentParser(
        description="LC-14b: ReconcileLoop real sources smoke test (detect-only)"
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Binance testnet instead of mainnet",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=10,
        help="Duration in seconds (default: 10)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3000,
        help="Reconcile interval in ms (default: 3000)",
    )
    parser.add_argument(
        "--skip-ws",
        action="store_true",
        help="Skip WebSocket connection test",
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  LC-14b: RECONCILE LOOP REAL SOURCES SMOKE (DETECT-ONLY)")
    print("=" * 60)
    print(f"  Network:     {'testnet' if args.testnet else 'mainnet'}")
    print(f"  Duration:    {args.duration}s")
    print(f"  Interval:    {args.interval}ms")
    print(f"  Skip WS:     {args.skip_ws}")
    print("=" * 60 + "\n")

    # Get API credentials
    api_key, api_secret = get_api_credentials(args.testnet)
    has_credentials = bool(api_key and api_secret)
    logger.info(
        "API credentials",
        extra={"available": has_credentials, "testnet": args.testnet},
    )

    # Track source status
    status = SourceStatus()

    # ==========================================================================
    # Test 1: REST Snapshot
    # ==========================================================================
    print("\n--- TEST 1: REST Snapshot ---")
    test_rest_snapshot(api_key, api_secret, args.testnet, status)

    # ==========================================================================
    # Test 2: Price Getter
    # ==========================================================================
    print("\n--- TEST 2: Price Getter ---")
    test_price_getter(args.testnet, status)

    # ==========================================================================
    # Test 3: WS Connection (optional)
    # ==========================================================================
    if not args.skip_ws:
        print("\n--- TEST 3: WS Connection ---")
        asyncio.run(test_ws_connection(api_key, api_secret, args.testnet, status))
    else:
        print("\n--- TEST 3: WS Connection (SKIPPED) ---")

    # ==========================================================================
    # Test 4: ReconcileLoop (detect-only proof)
    # ==========================================================================
    print("\n--- TEST 4: ReconcileLoop (detect-only) ---")
    fake_port = FakePort()
    loop_results = run_reconcile_loop_smoke(args.duration, args.interval, fake_port)

    # ==========================================================================
    # Summary
    # ==========================================================================
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)

    print("\n  Data Sources:")
    print(f"    REST snapshot:  {'OK' if status.rest_snapshot_ok else 'FAILED'}")
    if status.rest_snapshot_ok:
        print(f"      - Orders:     {status.rest_orders_count}")
        print(f"      - Positions:  {status.rest_positions_count}")
    elif status.rest_error:
        print(f"      - Error:      {status.rest_error}")

    print(f"    Price getter:   {'OK' if status.price_getter_ok else 'FAILED'}")
    if status.price_getter_ok:
        print(f"      - BTCUSDT:    ${status.price_fetched}")
    elif status.price_error:
        print(f"      - Error:      {status.price_error}")

    if not args.skip_ws:
        print(f"    WS connection:  {'OK' if status.ws_connected else 'FAILED'}")
        if status.ws_connected:
            print(f"      - Events:     {status.ws_events_received}")
        elif status.ws_error:
            print(f"      - Error:      {status.ws_error}")

    print("\n  ReconcileLoop:")
    print(f"    Total runs:     {loop_results['runs_total']}")
    print(f"    With mismatch:  {loop_results['runs_with_mismatch']}")
    print(f"    With error:     {loop_results['runs_with_error']}")
    print(f"    Port calls:     {loop_results['port_calls']}")

    print("\n" + "=" * 60)

    # Verify detect-only mode
    if loop_results["port_calls"] == 0:
        print("  ✓ DETECT-ONLY MODE VERIFIED: Zero port calls")
        print("=" * 60 + "\n")
        return 0
    else:
        print("  ✗ DETECT-ONLY VIOLATION: Port calls detected!")
        for call in fake_port.calls:
            print(f"    - {call}")
        print("=" * 60 + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
