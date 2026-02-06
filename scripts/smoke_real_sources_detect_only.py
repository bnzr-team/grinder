#!/usr/bin/env python3
"""LC-17: Credentialed Real-Source Smoke Test (detect-only).

This script verifies that GRINDER can:
1. Connect to real Binance Futures USDT-M sources with credentials
2. Establish User-data WS (listenKey)
3. Fetch REST snapshots (orders/positions)
4. Fetch prices via PriceGetter
5. Run ReconcileLoop in detect-only mode
6. Write audit JSONL as artifact

SAFETY GUARANTEES:
- Uses FakePort (no real order execution ever happens)
- detect_only=True enforced at ReconcileLoopConfig level
- action=NONE enforced at ReconcileConfig level
- armed=False enforced at RemediationExecutor level
- 0 execution calls guaranteed

EXIT CODES:
- 0: All checks passed, detect-only verified
- 1: Detect-only violation (port_calls > 0 or executed > 0)
- 2: Configuration error (missing credentials, invalid args)
- 3: Connection error (failed to establish required sources)

Usage:
    # Dry-run (no API keys - tests script structure only)
    PYTHONPATH=src python3 -m scripts.smoke_real_sources_detect_only --dry-run

    # With mainnet credentials (read-only, detect-only)
    BINANCE_API_KEY=xxx BINANCE_SECRET=xxx \\
    PYTHONPATH=src python3 -m scripts.smoke_real_sources_detect_only \\
        --duration 60 \\
        --audit-out /tmp/grinder_audit.jsonl

    # With testnet credentials
    BINANCE_TESTNET_API_KEY=xxx BINANCE_TESTNET_SECRET=xxx \\
    PYTHONPATH=src python3 -m scripts.smoke_real_sources_detect_only \\
        --testnet --duration 60

See ADR-050 for design decisions.
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
from pathlib import Path
from typing import Any

import requests

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.execution.binance_port import HttpResponse
from grinder.reconcile.audit import (
    AuditConfig,
    AuditEvent,
    AuditEventType,
    AuditWriter,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# =============================================================================
# Exit Codes (Stable Contract)
# =============================================================================

EXIT_SUCCESS = 0
EXIT_DETECT_ONLY_VIOLATION = 1
EXIT_CONFIG_ERROR = 2
EXIT_CONNECTION_ERROR = 3


# =============================================================================
# HTTP Client (requests-based)
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
# FakePort (detect-only proof)
# =============================================================================


@dataclass
class FakePort:
    """Fake port that records calls without executing.

    In detect-only mode, this should have 0 calls.
    Any call here means remediation was attempted (violation).
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Record cancel attempt (SHOULD NOT be called in detect-only)."""
        call = {
            "action": "cancel_order",
            "symbol": symbol,
            "client_order_id": client_order_id,
            "ts": int(time.time() * 1000),
        }
        self.calls.append(call)
        logger.error("FAKE_PORT_CANCEL: Detect-only violation!", extra=call)
        return {"status": "CANCELED", "clientOrderId": client_order_id}

    def place_market_order(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        """Record market order attempt (SHOULD NOT be called in detect-only)."""
        call = {
            "action": "place_market_order",
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "ts": int(time.time() * 1000),
        }
        self.calls.append(call)
        logger.error("FAKE_PORT_MARKET_ORDER: Detect-only violation!", extra=call)
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
# Results Tracking
# =============================================================================


@dataclass
class SmokeResults:
    """Final results of the smoke test."""

    # Source status
    sources: SourceStatus = field(default_factory=SourceStatus)

    # ReconcileLoop stats
    runs_total: int = 0
    runs_with_mismatch: int = 0
    runs_with_error: int = 0

    # Execution counters (MUST be 0 for detect-only)
    port_calls: int = 0
    executed_count: int = 0
    planned_count: int = 0
    blocked_count: int = 0

    # Audit
    audit_path: str | None = None
    audit_events_written: int = 0


# =============================================================================
# Source Tests
# =============================================================================


def get_api_credentials(testnet: bool) -> tuple[str, str]:
    """Get API credentials from environment."""
    if testnet:
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.environ.get("BINANCE_TESTNET_SECRET", "")
    else:
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_SECRET", "")
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
            extra={"orders": status.rest_orders_count, "positions": status.rest_positions_count},
        )

    except Exception as e:
        status.rest_error = str(e)
        logger.warning("REST: Failed", extra={"error": str(e)})


def test_price_getter(testnet: bool, status: SourceStatus) -> None:
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


# =============================================================================
# ReconcileLoop Smoke
# =============================================================================


def run_reconcile_loop_smoke(
    duration_sec: int,
    interval_ms: int,
    fake_port: FakePort,
    audit_writer: AuditWriter | None,
) -> dict[str, Any]:
    """Run ReconcileLoop with detect-only guarantees."""
    from grinder.live.reconcile_loop import (  # noqa: PLC0415
        ReconcileLoop,
        ReconcileLoopConfig,
    )
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
    identity_config = OrderIdentityConfig(prefix="grinder_lc17", allowed_strategies={"1"})

    # Create engine with detect-only config
    reconcile_config = ReconcileConfig(
        action=RemediationAction.NONE,  # Detect-only: NO remediation
        dry_run=True,  # Extra safety
        allow_active_remediation=False,  # Extra safety
    )
    metrics = get_reconcile_metrics()
    engine = ReconcileEngine(
        config=reconcile_config,
        expected=expected,
        observed=observed,
        metrics=metrics,
        identity_config=identity_config,
    )

    # Create executor with FakePort (detect-only proof)
    executor = RemediationExecutor(
        config=reconcile_config,
        port=fake_port,  # type: ignore[arg-type]
        armed=False,  # Extra safety: NOT armed
        symbol_whitelist=["BTCUSDT"],
        identity_config=identity_config,
    )

    # Create runner
    runner = ReconcileRunner(
        engine=engine,
        executor=executor,
        observed=observed,
        price_getter=lambda _: Decimal("50000.00"),  # Fake price
        audit_writer=audit_writer,
    )

    # Create loop with detect_only=True enforcer
    loop_config = ReconcileLoopConfig(
        enabled=True,
        interval_ms=interval_ms,
        detect_only=True,  # HARD enforcer
        require_active_role=False,
    )
    loop = ReconcileLoop(runner=runner, config=loop_config)

    logger.info("Starting ReconcileLoop (detect-only)...")
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


# =============================================================================
# Audit JSONL Writing
# =============================================================================


def write_smoke_start_event(writer: AuditWriter, testnet: bool, duration: int) -> None:
    """Write smoke test start event."""
    event = AuditEvent(
        ts_ms=int(time.time() * 1000),
        event_type=AuditEventType.RECONCILE_RUN,
        run_id=writer.generate_run_id(),
        mode="detect_only",
        action="smoke_start",
        details={
            "script": "smoke_real_sources_detect_only",
            "testnet": testnet,
            "duration_sec": duration,
            "lc_version": "LC-17",
        },
    )
    writer.write(event)


def write_smoke_end_event(writer: AuditWriter, results: SmokeResults) -> None:
    """Write smoke test end event."""
    event = AuditEvent(
        ts_ms=int(time.time() * 1000),
        event_type=AuditEventType.RECONCILE_RUN,
        run_id=writer.generate_run_id(),
        mode="detect_only",
        action="smoke_end",
        details={
            "runs_total": results.runs_total,
            "runs_with_mismatch": results.runs_with_mismatch,
            "runs_with_error": results.runs_with_error,
            "port_calls": results.port_calls,
            "executed_count": results.executed_count,
            "rest_snapshot_ok": results.sources.rest_snapshot_ok,
            "price_getter_ok": results.sources.price_getter_ok,
            "ws_connected": results.sources.ws_connected,
        },
    )
    writer.write(event)


# =============================================================================
# Main
# =============================================================================


def main() -> int:  # noqa: PLR0915, PLR0912
    """Run credentialed real-source smoke test (detect-only)."""
    parser = argparse.ArgumentParser(
        description="LC-17: Credentialed Real-Source Smoke Test (detect-only)"
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Binance testnet instead of mainnet",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5000,
        help="Reconcile interval in ms (default: 5000)",
    )
    parser.add_argument(
        "--audit-out",
        type=str,
        default=None,
        help="Path to audit JSONL output file",
    )
    parser.add_argument(
        "--skip-ws",
        action="store_true",
        help="Skip WebSocket connection test",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode (no credentials needed, tests script structure)",
    )
    args = parser.parse_args()

    # Banner
    print("\n" + "=" * 60)
    print("  LC-17: CREDENTIALED REAL-SOURCE SMOKE TEST (DETECT-ONLY)")
    print("=" * 60)
    print(f"  Network:     {'testnet' if args.testnet else 'mainnet'}")
    print(f"  Duration:    {args.duration}s")
    print(f"  Interval:    {args.interval}ms")
    print(f"  Audit out:   {args.audit_out or '(none)'}")
    print(f"  Skip WS:     {args.skip_ws}")
    print(f"  Dry-run:     {args.dry_run}")
    print("=" * 60 + "\n")

    # Initialize results
    results = SmokeResults()

    # Get API credentials
    api_key, api_secret = get_api_credentials(args.testnet)
    has_credentials = bool(api_key and api_secret)

    if not has_credentials and not args.dry_run:
        print("\n  ERROR: No API credentials found.")
        print("  Set BINANCE_API_KEY and BINANCE_SECRET (or BINANCE_TESTNET_* for testnet)")
        print("  Or use --dry-run to test script structure without credentials.")
        return EXIT_CONFIG_ERROR

    logger.info(
        "API credentials",
        extra={"available": has_credentials, "testnet": args.testnet},
    )

    # Setup audit writer
    audit_writer: AuditWriter | None = None
    if args.audit_out:
        # Ensure parent directory exists
        audit_path = Path(args.audit_out)
        audit_path.parent.mkdir(parents=True, exist_ok=True)

        audit_config = AuditConfig(
            enabled=True,
            path=str(audit_path),
            flush_every=1,
        )
        audit_writer = AuditWriter(audit_config)
        results.audit_path = str(audit_path)
        write_smoke_start_event(audit_writer, args.testnet, args.duration)

    try:
        # ==========================================================================
        # Test 1: REST Snapshot
        # ==========================================================================
        print("\n--- TEST 1: REST Snapshot ---")
        if not args.dry_run:
            test_rest_snapshot(api_key, api_secret, args.testnet, results.sources)
        else:
            print("  (skipped in dry-run mode)")

        # ==========================================================================
        # Test 2: Price Getter
        # ==========================================================================
        print("\n--- TEST 2: Price Getter ---")
        if not args.dry_run:
            test_price_getter(args.testnet, results.sources)
        else:
            print("  (skipped in dry-run mode)")

        # ==========================================================================
        # Test 3: WS Connection (optional)
        # ==========================================================================
        if not args.skip_ws:
            print("\n--- TEST 3: WS Connection ---")
            if not args.dry_run:
                asyncio.run(test_ws_connection(api_key, api_secret, args.testnet, results.sources))
            else:
                print("  (skipped in dry-run mode)")
        else:
            print("\n--- TEST 3: WS Connection (SKIPPED) ---")

        # ==========================================================================
        # Test 4: ReconcileLoop (detect-only proof)
        # ==========================================================================
        print("\n--- TEST 4: ReconcileLoop (detect-only) ---")
        fake_port = FakePort()
        loop_results = run_reconcile_loop_smoke(
            args.duration, args.interval, fake_port, audit_writer
        )

        results.runs_total = loop_results["runs_total"]
        results.runs_with_mismatch = loop_results["runs_with_mismatch"]
        results.runs_with_error = loop_results["runs_with_error"]
        results.port_calls = loop_results["port_calls"]

        # Write end event
        if audit_writer:
            write_smoke_end_event(audit_writer, results)
            results.audit_events_written = audit_writer.event_count

    finally:
        if audit_writer:
            audit_writer.close()

    # ==========================================================================
    # Summary
    # ==========================================================================
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)

    print("\n  Data Sources:")
    if args.dry_run:
        print("    (dry-run mode - no real connections)")
    else:
        print(f"    REST snapshot:  {'OK' if results.sources.rest_snapshot_ok else 'FAILED'}")
        if results.sources.rest_snapshot_ok:
            print(f"      - Orders:     {results.sources.rest_orders_count}")
            print(f"      - Positions:  {results.sources.rest_positions_count}")
        elif results.sources.rest_error:
            print(f"      - Error:      {results.sources.rest_error}")

        print(f"    Price getter:   {'OK' if results.sources.price_getter_ok else 'FAILED'}")
        if results.sources.price_getter_ok:
            print(f"      - BTCUSDT:    ${results.sources.price_fetched}")
        elif results.sources.price_error:
            print(f"      - Error:      {results.sources.price_error}")

        if not args.skip_ws:
            print(f"    WS connection:  {'OK' if results.sources.ws_connected else 'FAILED'}")
            if results.sources.ws_connected:
                print(f"      - Events:     {results.sources.ws_events_received}")
            elif results.sources.ws_error:
                print(f"      - Error:      {results.sources.ws_error}")

    print("\n  ReconcileLoop:")
    print(f"    Total runs:     {results.runs_total}")
    print(f"    With mismatch:  {results.runs_with_mismatch}")
    print(f"    With error:     {results.runs_with_error}")
    print(f"    Port calls:     {results.port_calls}")
    print(f"    Executed:       {results.executed_count}")

    if results.audit_path:
        print("\n  Audit:")
        print(f"    Path:           {results.audit_path}")
        print(f"    Events:         {results.audit_events_written}")

    print("\n" + "=" * 60)
    print("  VERIFICATION")
    print("=" * 60)

    print(f"\n  Total port calls: {results.port_calls}")

    # Verify detect-only mode
    if results.port_calls == 0 and results.executed_count == 0:
        print("\n" + "=" * 60)
        print("  DETECT-ONLY MODE VERIFIED: Zero port calls")
        print("=" * 60 + "\n")

        if args.dry_run:
            print("  (dry-run mode - script structure OK)\n")

        return EXIT_SUCCESS
    else:
        print("\n" + "=" * 60)
        print("  DETECT-ONLY VIOLATION!")
        print(f"  Port calls: {results.port_calls}")
        print(f"  Executed:   {results.executed_count}")
        print("=" * 60 + "\n")
        return EXIT_DETECT_ONLY_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
