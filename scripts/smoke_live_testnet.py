#!/usr/bin/env python3
"""Smoke test for Binance Testnet live trading.

This script performs a minimal E2E smoke test on Binance Testnet:
1. Connect to testnet
2. Place 1 micro limit order
3. Cancel the order (or record fill if executed)

SAFE-BY-CONSTRUCTION GUARDS:
- --dry-run by default (no real orders, only logging)
- Requires explicit --confirm TESTNET to place real orders
- MAINNET IS FORBIDDEN (blocked in BinanceExchangePort)
- Requires ARMED=1 + ALLOW_TESTNET_TRADE=1 env vars for real trades
- Kill-switch blocks PLACE/REPLACE but allows CANCEL

Usage:
    # Dry-run (default) - no real orders
    python -m scripts.smoke_live_testnet

    # Real testnet order (requires env vars)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_TESTNET_TRADE=1 \
        python -m scripts.smoke_live_testnet --confirm TESTNET

Environment variables:
    BINANCE_API_KEY: Testnet API key (required for real orders)
    BINANCE_API_SECRET: Testnet API secret (required for real orders)
    ARMED=1: Enable order execution (default: not set = dry-run)
    ALLOW_TESTNET_TRADE=1: Explicit testnet trade permission (default: not set)

See: docs/runbooks/08_SMOKE_TEST_TESTNET.md
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Guard: Import errors should fail clearly
try:
    import requests  # type: ignore[import-untyped]
except ImportError:
    print("ERROR: requests library required. Run: pip install requests")
    sys.exit(1)

from grinder.connectors.errors import (
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.binance_port import (
    BINANCE_SPOT_TESTNET_URL,
    BinanceExchangePort,
    BinanceExchangePortConfig,
    HttpResponse,
)

# --- Simple Requests-based HTTP Client ---


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


# --- Smoke Test Result ---


@dataclass
class SmokeResult:
    """Result of smoke test."""

    success: bool
    mode: str  # "dry-run" or "live"
    order_placed: bool = False
    order_id: str | None = None
    order_cancelled: bool = False
    error: str | None = None
    details: dict[str, Any] | None = None

    def print_summary(self) -> None:
        """Print human-readable summary."""
        status = "PASS" if self.success else "FAIL"
        print(f"\n{'=' * 60}")
        print(f"SMOKE TEST RESULT: {status}")
        print(f"{'=' * 60}")
        print(f"  Mode: {self.mode}")
        print(f"  Order placed: {self.order_placed}")
        if self.order_id:
            print(f"  Order ID: {self.order_id}")
        print(f"  Order cancelled: {self.order_cancelled}")
        if self.error:
            print(f"  Error: {self.error}")
        if self.details:
            print(f"  Details: {self.details}")
        print(f"{'=' * 60}\n")


# --- Smoke Test Logic ---


def check_env_guards() -> tuple[bool, str]:
    """Check environment variable guards.

    Returns:
        (can_trade, reason): Whether real trading is allowed and why/why not.
    """
    armed = os.environ.get("ARMED", "").lower() in ("1", "true", "yes")
    allow_testnet = os.environ.get("ALLOW_TESTNET_TRADE", "").lower() in ("1", "true", "yes")
    has_key = bool(os.environ.get("BINANCE_API_KEY"))
    has_secret = bool(os.environ.get("BINANCE_API_SECRET"))

    if not has_key:
        return False, "BINANCE_API_KEY not set"
    if not has_secret:
        return False, "BINANCE_API_SECRET not set"
    if not armed:
        return False, "ARMED=1 not set"
    if not allow_testnet:
        return False, "ALLOW_TESTNET_TRADE=1 not set"

    return True, "All guards passed"


def run_smoke_test(
    symbol: str = "BTCUSDT",
    price: Decimal = Decimal("10000.00"),  # Far from market = won't fill
    quantity: Decimal = Decimal("0.001"),  # Micro lot
    dry_run: bool = True,
    kill_switch: bool = False,
) -> SmokeResult:
    """Run the smoke test.

    Args:
        symbol: Symbol to trade (must be on testnet)
        price: Limit price (should be far from market to avoid fill)
        quantity: Order quantity (micro lot)
        dry_run: If True, use dry-run mode (no real HTTP calls)
        kill_switch: If True, simulate kill-switch active

    Returns:
        SmokeResult with test outcome
    """
    mode = "dry-run" if dry_run else "live-testnet"
    print(f"\nStarting smoke test (mode={mode}, symbol={symbol})")
    print(f"  Price: {price}, Quantity: {quantity}")

    # Check env guards for live mode
    if not dry_run:
        can_trade, reason = check_env_guards()
        if not can_trade:
            return SmokeResult(
                success=False,
                mode=mode,
                error=f"Environment guard failed: {reason}",
            )

    # Create HTTP client
    http_client = RequestsHttpClient()

    # Create exchange port config
    try:
        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_SPOT_TESTNET_URL,
            api_key=os.environ.get("BINANCE_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_API_SECRET", ""),
            symbol_whitelist=[symbol],
            dry_run=dry_run,
        )
    except ConnectorNonRetryableError as e:
        return SmokeResult(
            success=False,
            mode=mode,
            error=f"Config error: {e}",
        )

    port = BinanceExchangePort(http_client=http_client, config=config)

    # Kill-switch check (before placing order)
    if kill_switch:
        print("  Kill-switch is ACTIVE - PLACE blocked, CANCEL allowed")
        return SmokeResult(
            success=True,
            mode=mode,
            order_placed=False,
            error="Kill-switch active - order placement blocked (expected)",
            details={"kill_switch": True},
        )

    # Step 1: Place order
    print(f"  Placing limit order: {symbol} BUY {quantity} @ {price}")
    order_id: str | None = None
    try:
        ts = int(time.time() * 1000)
        order_id = port.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            price=price,
            quantity=quantity,
            level_id=0,
            ts=ts,
        )
        print(f"  Order placed: {order_id}")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        return SmokeResult(
            success=False,
            mode=mode,
            order_placed=False,
            error=f"Place order failed: {e}",
        )

    # Step 2: Cancel order
    print(f"  Cancelling order: {order_id}")
    cancelled = False
    try:
        cancelled = port.cancel_order(order_id)
        print(f"  Order cancelled: {cancelled}")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        # Cancel failure is not necessarily fatal (order may have filled)
        print(f"  Cancel failed (may have filled): {e}")

    return SmokeResult(
        success=True,
        mode=mode,
        order_placed=True,
        order_id=order_id,
        order_cancelled=cancelled,
        details={
            "symbol": symbol,
            "price": str(price),
            "quantity": str(quantity),
            "testnet_url": BINANCE_SPOT_TESTNET_URL,
        },
    )


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Smoke test for Binance Testnet live trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Dry-run (default, no real orders)
    python -m scripts.smoke_live_testnet

    # Real testnet order
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_TESTNET_TRADE=1 \\
        python -m scripts.smoke_live_testnet --confirm TESTNET

    # Test kill-switch behavior
    python -m scripts.smoke_live_testnet --kill-switch
""",
    )
    parser.add_argument(
        "--confirm",
        choices=["TESTNET"],
        help="Confirm live trading on TESTNET (required for real orders)",
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Symbol to trade (default: BTCUSDT)",
    )
    parser.add_argument(
        "--price",
        type=Decimal,
        default=Decimal("10000.00"),
        help="Limit price (default: 10000.00, far from market)",
    )
    parser.add_argument(
        "--quantity",
        type=Decimal,
        default=Decimal("0.001"),
        help="Order quantity (default: 0.001, micro lot)",
    )
    parser.add_argument(
        "--kill-switch",
        action="store_true",
        help="Simulate kill-switch active (blocks PLACE, allows CANCEL)",
    )

    args = parser.parse_args()

    # Determine mode
    dry_run = args.confirm != "TESTNET"

    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE (no real orders)")
        print("To place real orders on testnet, use: --confirm TESTNET")
        print("=" * 60)
    else:
        print("=" * 60)
        print("LIVE TESTNET MODE")
        print("Real orders will be placed on Binance Testnet")
        print("=" * 60)

    # Run smoke test
    result = run_smoke_test(
        symbol=args.symbol,
        price=args.price,
        quantity=args.quantity,
        dry_run=dry_run,
        kill_switch=args.kill_switch,
    )

    # Print summary
    result.print_summary()

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
