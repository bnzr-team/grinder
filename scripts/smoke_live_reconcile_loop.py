#!/usr/bin/env python3
"""Smoke test for ReconcileLoop integration (LC-14a).

Demonstrates ReconcileLoop running in detect-only mode with FakePort.
No real HTTP/WS calls, safe to run anywhere.

Usage:
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop

    # With custom duration
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop --duration 30

    # With injected mismatches
    PYTHONPATH=src python3 -m scripts.smoke_live_reconcile_loop --inject-mismatch
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from grinder.core import OrderSide, OrderState
from grinder.live.reconcile_loop import ReconcileLoop, ReconcileLoopConfig
from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.identity import OrderIdentityConfig, generate_client_order_id
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.remediation import RemediationExecutor
from grinder.reconcile.runner import ReconcileRunner
from grinder.reconcile.types import ObservedOrder, ObservedPosition

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# FAKE PORT
# =============================================================================


@dataclass
class FakePort:
    """Fake exchange port for testing (no real HTTP).

    Records all calls for verification.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Record cancel call."""
        self.calls.append(
            {
                "method": "cancel_order",
                "symbol": symbol,
                "client_order_id": client_order_id,
                "ts": int(time.time() * 1000),
            }
        )
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "status": "CANCELED",
            "orderId": 123456789,
        }

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Record market order call."""
        self.calls.append(
            {
                "method": "place_market_order",
                "symbol": symbol,
                "side": side.value,
                "qty": str(qty),
                "reduce_only": reduce_only,
                "ts": int(time.time() * 1000),
            }
        )
        return {
            "symbol": symbol,
            "side": side.value,
            "type": "MARKET",
            "origQty": str(qty),
            "executedQty": str(qty),
            "status": "FILLED",
            "orderId": 123456790,
        }


# =============================================================================
# MISMATCH INJECTION
# =============================================================================


def inject_unexpected_order(
    observed: ObservedStateStore,
    identity_config: OrderIdentityConfig,
    symbol: str = "BTCUSDT",
) -> None:
    """Inject an unexpected order into observed state."""
    ts = int(time.time() * 1000)
    cid = generate_client_order_id(identity_config, symbol, 1, ts, 1)
    order = ObservedOrder(
        client_order_id=cid,
        symbol=symbol,
        order_id=123456789,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("50000.00"),
        orig_qty=Decimal("0.001"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=ts,
        source="test",
    )
    observed._orders[cid] = order
    observed._last_snapshot_ts = ts
    logger.info(f"Injected unexpected order: {cid}")


def inject_unexpected_position(
    observed: ObservedStateStore,
    symbol: str = "BTCUSDT",
) -> None:
    """Inject an unexpected position into observed state."""
    ts = int(time.time() * 1000)
    position = ObservedPosition(
        symbol=symbol,
        position_amt=Decimal("0.001"),
        entry_price=Decimal("50000.00"),
        unrealized_pnl=Decimal("0.10"),
        ts_observed=ts,
        source="test",
    )
    observed._positions[symbol] = position
    observed._last_snapshot_ts = ts
    logger.info(f"Injected unexpected position: {symbol}")


# =============================================================================
# MAIN
# =============================================================================


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Smoke test for ReconcileLoop (detect-only mode)")
    parser.add_argument(
        "--duration",
        type=int,
        default=15,
        help="Duration in seconds (default: 15)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=3000,
        help="Reconcile interval in ms (default: 3000)",
    )
    parser.add_argument(
        "--inject-mismatch",
        action="store_true",
        help="Inject a mismatch for testing",
    )
    return parser.parse_args()


def main() -> int:  # noqa: PLR0915
    """Run smoke test."""
    args = parse_args()

    print("\n" + "=" * 60)
    print("  RECONCILE LOOP SMOKE TEST (LC-14a)")
    print("=" * 60)
    print(f"  Duration:       {args.duration}s")
    print(f"  Interval:       {args.interval}ms")
    print(f"  Inject mismatch: {args.inject_mismatch}")
    print("=" * 60 + "\n")

    # Setup components
    identity_config = OrderIdentityConfig(
        prefix="grinder_",
        strategy_id="smoke",
    )
    symbol_whitelist = ["BTCUSDT"]

    observed = ObservedStateStore()
    expected = ExpectedStateStore()

    # Config: detect-only mode
    reconcile_config = ReconcileConfig(
        enabled=True,
        action=RemediationAction.NONE,  # Detect-only
        dry_run=True,
        allow_active_remediation=False,
    )

    # Fake port (no real HTTP)
    fake_port = FakePort()

    # Engine
    engine = ReconcileEngine(
        config=reconcile_config,
        expected=expected,
        observed=observed,
        identity_config=identity_config,
    )

    # Executor
    executor = RemediationExecutor(
        config=reconcile_config,
        port=fake_port,  # type: ignore[arg-type]
        armed=False,
        symbol_whitelist=symbol_whitelist,
        identity_config=identity_config,
    )

    # Runner
    runner = ReconcileRunner(
        engine=engine,
        executor=executor,
        observed=observed,
    )

    # Loop config (enabled with short interval)
    loop_config = ReconcileLoopConfig(
        enabled=True,
        interval_ms=args.interval,
        require_active_role=False,  # Don't check HA for smoke test
    )

    # Create loop
    loop = ReconcileLoop(
        runner=runner,
        config=loop_config,
    )

    # Inject mismatch if requested
    if args.inject_mismatch:
        inject_unexpected_order(observed, identity_config)

    # Setup shutdown signal
    shutdown_event = threading.Event()

    def signal_handler(_signum: int, _frame: Any) -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start loop
    logger.info("Starting ReconcileLoop...")
    loop.start()

    # Wait for duration or shutdown
    start_time = time.time()
    try:
        while not shutdown_event.is_set():
            elapsed = time.time() - start_time
            if elapsed >= args.duration:
                break
            shutdown_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    # Stop loop
    logger.info("Stopping ReconcileLoop...")
    loop.stop()

    # Print summary
    stats = loop.stats
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Total runs:          {stats.runs_total}")
    print(f"  Runs with mismatch:  {stats.runs_with_mismatch}")
    print(f"  Runs skipped (role): {stats.runs_skipped_role}")
    print(f"  Runs with error:     {stats.runs_with_error}")
    print(f"  Last run timestamp:  {stats.last_run_ts_ms}")
    print(f"  Port calls:          {len(fake_port.calls)}")

    if stats.last_report:
        print("\n  Last report:")
        print(f"    Mismatches:        {stats.last_report.mismatches_detected}")
        print(f"    Planned count:     {stats.last_report.planned_count}")
        print(f"    Executed count:    {stats.last_report.executed_count}")
        print(f"    Blocked count:     {stats.last_report.blocked_count}")

    print("=" * 60)

    # Verify detect-only mode
    if len(fake_port.calls) == 0:
        print("\n  ✓ DETECT-ONLY MODE VERIFIED: Zero port calls")
        print("=" * 60 + "\n")
        return 0
    else:
        print(f"\n  ✗ UNEXPECTED PORT CALLS: {len(fake_port.calls)}")
        for call in fake_port.calls:
            print(f"    - {call}")
        print("=" * 60 + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
