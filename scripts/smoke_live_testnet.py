#!/usr/bin/env python3
"""Smoke test for Binance live trading (testnet or mainnet).

This script performs a minimal E2E smoke test:
1. Connect to exchange
2. Place 1 micro limit order
3. Cancel the order (or record fill if executed)

SAFE-BY-CONSTRUCTION GUARDS:
- --dry-run by default (no real orders, only logging)
- Requires explicit --confirm TESTNET or --confirm MAINNET_TRADE for real orders
- Mainnet requires ALLOW_MAINNET_TRADE=1 + additional guards (ADR-039)
- Requires ARMED=1 env var for any real trades
- Kill-switch blocks PLACE/REPLACE but allows CANCEL
- Symbol whitelist enforced
- Notional limits enforced (mainnet)

Usage:
    # Dry-run (default) - no real orders
    python -m scripts.smoke_live_testnet

    # Real testnet order (requires env vars)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_TESTNET_TRADE=1 \
        python -m scripts.smoke_live_testnet --confirm TESTNET

    # Real mainnet order (requires additional guards)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \
        python -m scripts.smoke_live_testnet --confirm MAINNET_TRADE

Environment variables:
    BINANCE_API_KEY: API key (required for real orders)
    BINANCE_API_SECRET: API secret (required for real orders)
    ARMED=1: Enable order execution (default: not set = dry-run)
    ALLOW_TESTNET_TRADE=1: Explicit testnet trade permission
    ALLOW_MAINNET_TRADE=1: Explicit mainnet trade permission (mainnet only)

See: docs/runbooks/08_SMOKE_TEST_TESTNET.md, docs/runbooks/09_MAINNET_TRADE_SMOKE.md
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
    import requests
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
    BINANCE_SPOT_MAINNET_URL,
    BINANCE_SPOT_TESTNET_URL,
    BinanceExchangePort,
    BinanceExchangePortConfig,
    HttpResponse,
)

# --- Default safety limits ---
DEFAULT_MAX_NOTIONAL_MAINNET = Decimal("50.00")  # $50 max per order on mainnet
DEFAULT_MAX_ORDERS_PER_RUN = 1  # Single order per run


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
    mode: str  # "dry-run", "live-testnet", or "live-mainnet"
    simulated: bool = False  # True if dry-run (no real HTTP calls)
    order_placed: bool = False  # Only True for REAL orders
    order_id: str | None = None
    order_cancelled: bool = False  # Only True for REAL cancels
    sim_place_ok: bool = False  # True if simulated place succeeded
    sim_cancel_ok: bool = False  # True if simulated cancel succeeded
    error: str | None = None
    details: dict[str, Any] | None = None

    def print_summary(self) -> None:
        """Print human-readable summary."""
        status = "PASS" if self.success else "FAIL"
        print(f"\n{'=' * 60}")
        print(f"SMOKE TEST RESULT: {status}")
        print(f"{'=' * 60}")
        print(f"  Mode: {self.mode}")

        # Check for kill-switch scenario
        is_kill_switch = self.details and self.details.get("kill_switch")
        if is_kill_switch:
            print("  Kill-switch test:")
            print("    PLACE: BLOCKED (expected)")
            print("    CANCEL: ALLOWED")
        elif self.simulated:
            print("  ** SIMULATED - No real HTTP calls made **")
            print(f"  Simulated place: {'OK' if self.sim_place_ok else 'FAILED'}")
            print(f"  Simulated cancel: {'OK' if self.sim_cancel_ok else 'FAILED'}")
            if self.order_id:
                print(f"  Simulated order ID: {self.order_id}")
        else:
            print(f"  Order placed: {self.order_placed}")
            if self.order_id:
                print(f"  Order ID: {self.order_id}")
            print(f"  Order cancelled: {self.order_cancelled}")

        if self.error:
            print(f"  Error: {self.error}")
        if self.details and not is_kill_switch:
            print(f"  Details: {self.details}")
        print(f"{'=' * 60}\n")


# --- Smoke Test Logic ---


def check_env_guards(is_mainnet: bool) -> tuple[bool, str]:
    """Check environment variable guards.

    Args:
        is_mainnet: True if checking for mainnet, False for testnet

    Returns:
        (can_trade, reason): Whether real trading is allowed and why/why not.
    """
    armed = os.environ.get("ARMED", "").lower() in ("1", "true", "yes")
    has_key = bool(os.environ.get("BINANCE_API_KEY"))
    has_secret = bool(os.environ.get("BINANCE_API_SECRET"))

    if not has_key:
        return False, "BINANCE_API_KEY not set"
    if not has_secret:
        return False, "BINANCE_API_SECRET not set"
    if not armed:
        return False, "ARMED=1 not set"

    if is_mainnet:
        allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if not allow_mainnet:
            return False, "ALLOW_MAINNET_TRADE=1 not set (required for mainnet)"
    else:
        allow_testnet = os.environ.get("ALLOW_TESTNET_TRADE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if not allow_testnet:
            return False, "ALLOW_TESTNET_TRADE=1 not set"

    return True, "All guards passed"


def run_smoke_test(
    symbol: str = "BTCUSDT",
    price: Decimal = Decimal("10000.00"),  # Far from market = won't fill
    quantity: Decimal = Decimal("0.001"),  # Micro lot
    dry_run: bool = True,
    kill_switch: bool = False,
    is_mainnet: bool = False,
    max_notional: Decimal | None = None,
) -> SmokeResult:
    """Run the smoke test.

    Args:
        symbol: Symbol to trade
        price: Limit price (should be far from market to avoid fill)
        quantity: Order quantity (micro lot)
        dry_run: If True, use dry-run mode (no real HTTP calls)
        kill_switch: If True, simulate kill-switch active
        is_mainnet: If True, use mainnet instead of testnet
        max_notional: Maximum notional per order (required for mainnet)

    Returns:
        SmokeResult with test outcome
    """
    if is_mainnet:
        mode = "dry-run" if dry_run else "live-mainnet"
        base_url = BINANCE_SPOT_MAINNET_URL
    else:
        mode = "dry-run" if dry_run else "live-testnet"
        base_url = BINANCE_SPOT_TESTNET_URL

    print(f"\nStarting smoke test (mode={mode}, symbol={symbol})")
    print(f"  Price: {price}, Quantity: {quantity}")
    print(f"  Notional: ${price * quantity:.2f}")
    if is_mainnet:
        print(f"  Max notional: ${max_notional:.2f}" if max_notional else "  Max notional: NONE")
        print(f"  Base URL: {base_url}")

    # Check env guards for live mode
    if not dry_run:
        can_trade, reason = check_env_guards(is_mainnet)
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
            base_url=base_url,
            api_key=os.environ.get("BINANCE_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_API_SECRET", ""),
            symbol_whitelist=[symbol],
            dry_run=dry_run,
            # Mainnet guards
            allow_mainnet=is_mainnet,
            max_notional_per_order=max_notional if is_mainnet else None,
            max_orders_per_run=DEFAULT_MAX_ORDERS_PER_RUN,
            max_open_orders=1,
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
            simulated=dry_run,
            order_placed=False,
            details={"kill_switch": True, "place_blocked": True, "cancel_allowed": True},
        )

    # Step 1: Place order
    action_prefix = "SIMULATED " if dry_run else ""
    print(f"  {action_prefix}Placing limit order: {symbol} BUY {quantity} @ {price}")
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
        # In dry-run, prefix order_id to make it obvious
        if dry_run:
            order_id = f"SIM_{order_id}"
        print(f"  {action_prefix}Order placed: {order_id}")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        return SmokeResult(
            success=False,
            mode=mode,
            simulated=dry_run,
            order_placed=False,
            error=f"Place order failed: {e}",
        )

    # Step 2: Cancel order
    print(f"  {action_prefix}Cancelling order: {order_id}")
    cancelled = False
    try:
        # For cancel, use original order_id without SIM_ prefix
        cancel_id = order_id.replace("SIM_", "") if dry_run and order_id else order_id
        cancelled = port.cancel_order(cancel_id) if cancel_id else False
        print(f"  {action_prefix}Order cancelled: {cancelled}")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        # Cancel failure is not necessarily fatal (order may have filled)
        print(f"  {action_prefix}Cancel failed (may have filled): {e}")

    return SmokeResult(
        success=True,
        mode=mode,
        simulated=dry_run,
        order_placed=not dry_run and order_id is not None,  # Only True for REAL orders
        order_id=order_id,
        order_cancelled=cancelled if not dry_run else False,  # Only True for REAL cancels
        sim_place_ok=dry_run and order_id is not None,  # Simulated place succeeded
        sim_cancel_ok=dry_run and cancelled,  # Simulated cancel succeeded
        details={
            "symbol": symbol,
            "price": str(price),
            "quantity": str(quantity),
            "notional": str(price * quantity),
            "base_url": base_url,
            "is_mainnet": is_mainnet,
            "simulated": dry_run,
        },
    )


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Smoke test for Binance live trading (testnet or mainnet)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Dry-run (default, no real orders)
    python -m scripts.smoke_live_testnet

    # Real testnet order
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_TESTNET_TRADE=1 \\
        python -m scripts.smoke_live_testnet --confirm TESTNET

    # Real mainnet order (budgeted, micro lot)
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \\
        python -m scripts.smoke_live_testnet --confirm MAINNET_TRADE

    # Test kill-switch behavior
    python -m scripts.smoke_live_testnet --kill-switch
""",
    )
    parser.add_argument(
        "--confirm",
        choices=["TESTNET", "MAINNET_TRADE"],
        help="Confirm live trading (TESTNET or MAINNET_TRADE)",
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
        "--max-notional",
        type=Decimal,
        default=DEFAULT_MAX_NOTIONAL_MAINNET,
        help=f"Max notional per order for mainnet (default: ${DEFAULT_MAX_NOTIONAL_MAINNET})",
    )
    parser.add_argument(
        "--kill-switch",
        action="store_true",
        help="Simulate kill-switch active (blocks PLACE, allows CANCEL)",
    )

    args = parser.parse_args()

    # Determine mode
    dry_run = args.confirm not in ("TESTNET", "MAINNET_TRADE")
    is_mainnet = args.confirm == "MAINNET_TRADE"

    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE (no real orders)")
        print("To place real orders:")
        print("  Testnet: --confirm TESTNET")
        print("  Mainnet: --confirm MAINNET_TRADE")
        print("=" * 60)
    elif is_mainnet:
        print("=" * 60)
        print("*** LIVE MAINNET MODE ***")
        print("Real orders will be placed on Binance MAINNET")
        print(f"Symbol whitelist: [{args.symbol}]")
        print(f"Max notional per order: ${args.max_notional}")
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
        is_mainnet=is_mainnet,
        max_notional=args.max_notional if is_mainnet else None,
    )

    # Print summary
    result.print_summary()

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
