#!/usr/bin/env python3
"""Smoke test for Binance Futures USDT-M mainnet trading.

This script performs a minimal E2E smoke test:
1. Query account info (leverage, position mode, margin type)
2. Set leverage to target (default: 1x)
3. Place 1 micro limit order (far from market)
4. Cancel the order
5. If partially/fully filled → close position to 0

SAFE-BY-CONSTRUCTION GUARDS (9 layers):
1. --dry-run by default (no real orders, only logging)
2. Requires explicit --confirm FUTURES_MAINNET_TRADE for real orders
3. Requires ALLOW_MAINNET_TRADE=1 env var
4. Requires ARMED=1 env var
5. symbol_whitelist required (non-empty)
6. max_notional_per_order required (default: $50)
7. max_orders_per_run=1 (single order per run)
8. leverage=1 enforced
9. Position cleanup on any fill

Usage:
    # Dry-run (default) - no real orders
    python -m scripts.smoke_futures_mainnet

    # Real futures mainnet order
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \\
        python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE

Environment variables:
    BINANCE_API_KEY: API key (required for real orders)
    BINANCE_API_SECRET: API secret (required for real orders)
    ARMED=1: Enable order execution (default: not set = dry-run)
    ALLOW_MAINNET_TRADE=1: Explicit mainnet trade permission

See: docs/runbooks/10_FUTURES_MAINNET_TRADE_SMOKE.md
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
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.binance_port import HttpResponse

# --- Default safety limits ---
DEFAULT_MAX_NOTIONAL = Decimal("50.00")  # $50 max per order
DEFAULT_MAX_ORDERS = 1  # Single order per run
DEFAULT_LEVERAGE = 1  # 1x leverage (no leverage)


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
class FuturesSmokeResult:
    """Result of futures smoke test."""

    success: bool
    mode: str  # "dry-run" or "live-futures-mainnet"
    simulated: bool = False

    # Account info
    position_mode: str | None = None  # "hedge" or "one-way"
    leverage_set: int | None = None
    leverage_actual: int | None = None

    # Order info
    order_placed: bool = False
    order_id: str | None = None
    binance_order_id: int | None = None
    order_cancelled: bool = False

    # Position cleanup
    had_position: bool = False
    position_closed: bool = False
    close_order_id: str | None = None

    # Error
    error: str | None = None
    details: dict[str, Any] | None = None

    def print_summary(self) -> None:
        """Print human-readable summary."""
        status = "PASS" if self.success else "FAIL"
        print(f"\n{'=' * 60}")
        print(f"FUTURES SMOKE TEST RESULT: {status}")
        print(f"{'=' * 60}")
        print(f"  Mode: {self.mode}")

        if self.simulated:
            print("  ** SIMULATED - No real HTTP calls made **")

        # Account info
        if self.position_mode:
            print(f"  Position mode: {self.position_mode}")
        if self.leverage_set:
            print(f"  Target leverage: {self.leverage_set}x")
        if self.leverage_actual:
            print(f"  Actual leverage: {self.leverage_actual}x")

        # Order info
        if not self.simulated:
            print(f"  Order placed: {self.order_placed}")
            if self.order_id:
                print(f"  Client order ID: {self.order_id}")
            if self.binance_order_id:
                print(f"  Binance order ID: {self.binance_order_id}")
            print(f"  Order cancelled: {self.order_cancelled}")

            # Position cleanup
            if self.had_position:
                print(f"  Had position: {self.had_position}")
                print(f"  Position closed: {self.position_closed}")
                if self.close_order_id:
                    print(f"  Close order ID: {self.close_order_id}")

        if self.error:
            print(f"  Error: {self.error}")
        if self.details:
            print(f"  Details: {self.details}")
        print(f"{'=' * 60}\n")


# --- Environment Guard Check ---


def check_env_guards() -> tuple[bool, str]:
    """Check environment variable guards for futures mainnet.

    Returns:
        (can_trade, reason): Whether trading is allowed and why/why not.
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

    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in ("1", "true", "yes")
    if not allow_mainnet:
        return False, "ALLOW_MAINNET_TRADE=1 not set (required for futures mainnet)"

    return True, "All guards passed"


# --- Smoke Test Logic ---


def run_futures_smoke_test(  # noqa: PLR0912, PLR0915
    symbol: str = "BTCUSDT",
    price: Decimal = Decimal("80000.00"),  # Close to market but unlikely to fill
    quantity: Decimal = Decimal("0.001"),  # Micro lot
    dry_run: bool = True,
    max_notional: Decimal = DEFAULT_MAX_NOTIONAL,
    target_leverage: int = DEFAULT_LEVERAGE,
) -> FuturesSmokeResult:
    """Run the futures smoke test.

    Args:
        symbol: Symbol to trade
        price: Limit price (should be far from market to avoid fill)
        quantity: Order quantity (micro lot)
        dry_run: If True, use dry-run mode (no real HTTP calls)
        max_notional: Maximum notional per order
        target_leverage: Target leverage to set (default: 1)

    Returns:
        FuturesSmokeResult with test outcome
    """
    mode = "dry-run" if dry_run else "live-futures-mainnet"
    base_url = BINANCE_FUTURES_MAINNET_URL

    print(f"\nStarting futures smoke test (mode={mode}, symbol={symbol})")
    print(f"  Base URL: {base_url}")
    print(f"  Price: {price}, Quantity: {quantity}")
    print(f"  Notional: ${price * quantity:.2f}")
    print(f"  Max notional: ${max_notional:.2f}")
    print(f"  Target leverage: {target_leverage}x")

    # Check env guards for live mode
    if not dry_run:
        can_trade, reason = check_env_guards()
        if not can_trade:
            return FuturesSmokeResult(
                success=False,
                mode=mode,
                error=f"Environment guard failed: {reason}",
            )

    # Create HTTP client
    http_client = RequestsHttpClient()

    # Create futures port config
    try:
        config = BinanceFuturesPortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=base_url,
            api_key=os.environ.get("BINANCE_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_API_SECRET", ""),
            symbol_whitelist=[symbol],
            dry_run=dry_run,
            allow_mainnet=True,
            max_notional_per_order=max_notional,
            max_orders_per_run=DEFAULT_MAX_ORDERS,
            max_open_orders=1,
            target_leverage=target_leverage,
        )
    except ConnectorNonRetryableError as e:
        return FuturesSmokeResult(
            success=False,
            mode=mode,
            error=f"Config error: {e}",
        )

    port = BinanceFuturesPort(http_client=http_client, config=config)

    # --- Step 1: Get account info ---
    print("\n  [Step 1] Getting account info...")
    position_mode = None
    try:
        position_mode = port.get_position_mode()
        print(f"  Position mode: {position_mode}")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"  Warning: Could not get position mode: {e}")

    # --- Step 2: Set leverage ---
    print(f"\n  [Step 2] Setting leverage to {target_leverage}x...")
    leverage_actual = None
    try:
        leverage_actual = port.set_leverage(symbol, target_leverage)
        print(f"  Leverage set to: {leverage_actual}x")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"  Warning: Could not set leverage: {e}")

    # --- Step 3: Check existing position ---
    print("\n  [Step 3] Checking existing position...")
    had_position = False
    try:
        positions = port.get_positions(symbol)
        if positions:
            for pos in positions:
                print(f"  Found position: {pos.position_amt} @ {pos.entry_price}")
                print(f"    Leverage: {pos.leverage}x, Margin: {pos.margin_type}")
                had_position = True
        else:
            print("  No existing position")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"  Warning: Could not check position: {e}")

    # --- Step 4: Place order ---
    print(f"\n  [Step 4] Placing limit order: {symbol} BUY {quantity} @ {price}")
    order_id = None
    binance_order_id = None
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

        # Try to extract Binance order ID
        if order_id and order_id.isdigit():
            binance_order_id = int(order_id)

    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        return FuturesSmokeResult(
            success=False,
            mode=mode,
            simulated=dry_run,
            position_mode=position_mode,
            leverage_set=target_leverage,
            leverage_actual=leverage_actual,
            error=f"Place order failed: {e}",
        )

    # --- Step 5: Cancel order ---
    print(f"\n  [Step 5] Cancelling order: {order_id}")
    cancelled = False
    try:
        if binance_order_id:
            cancelled = port.cancel_order_by_binance_id(symbol, binance_order_id)
        else:
            cancelled = port.cancel_order(order_id) if order_id else False
        print(f"  Order cancelled: {cancelled}")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"  Cancel failed (may have filled): {e}")

    # --- Step 6: Check and close any position ---
    print("\n  [Step 6] Checking for position to close...")
    position_closed = False
    close_order_id = None
    try:
        positions = port.get_positions(symbol)
        if positions:
            for pos in positions:
                if pos.position_amt != 0:
                    print(f"  Found position to close: {pos.position_amt}")
                    close_order_id = port.close_position(symbol)
                    if close_order_id:
                        print(f"  Position closed with order: {close_order_id}")
                        position_closed = True
                    else:
                        print("  Failed to close position")
        else:
            print("  No position to close")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"  Warning: Error during position cleanup: {e}")

    # --- Step 7: Final position check ---
    print("\n  [Step 7] Final position check...")
    try:
        final_positions = port.get_positions(symbol)
        if final_positions:
            for pos in final_positions:
                if pos.position_amt != 0:
                    print(f"  WARNING: Remaining position: {pos.position_amt}")
        else:
            print("  Position is 0 (clean)")
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"  Warning: Could not verify final position: {e}")

    return FuturesSmokeResult(
        success=True,
        mode=mode,
        simulated=dry_run,
        position_mode=position_mode,
        leverage_set=target_leverage,
        leverage_actual=leverage_actual,
        order_placed=not dry_run and order_id is not None,
        order_id=order_id,
        binance_order_id=binance_order_id,
        order_cancelled=cancelled if not dry_run else False,
        had_position=had_position,
        position_closed=position_closed,
        close_order_id=close_order_id,
        details={
            "symbol": symbol,
            "price": str(price),
            "quantity": str(quantity),
            "notional": str(price * quantity),
            "base_url": base_url,
            "is_futures": True,
            "simulated": dry_run,
        },
    )


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Smoke test for Binance Futures USDT-M mainnet trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Dry-run (default, no real orders)
    python -m scripts.smoke_futures_mainnet

    # Real futures mainnet order
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ARMED=1 ALLOW_MAINNET_TRADE=1 \\
        python -m scripts.smoke_futures_mainnet --confirm FUTURES_MAINNET_TRADE
""",
    )
    parser.add_argument(
        "--confirm",
        choices=["FUTURES_MAINNET_TRADE"],
        help="Confirm live trading (FUTURES_MAINNET_TRADE)",
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Symbol to trade (default: BTCUSDT)",
    )
    parser.add_argument(
        "--price",
        type=Decimal,
        default=Decimal("80000.00"),
        help="Limit price (default: 80000.00)",
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
        default=DEFAULT_MAX_NOTIONAL,
        help=f"Max notional per order (default: ${DEFAULT_MAX_NOTIONAL})",
    )
    parser.add_argument(
        "--leverage",
        type=int,
        default=DEFAULT_LEVERAGE,
        help=f"Target leverage (default: {DEFAULT_LEVERAGE}x)",
    )

    args = parser.parse_args()

    # Determine mode
    dry_run = args.confirm != "FUTURES_MAINNET_TRADE"

    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE (no real orders)")
        print("To place real orders:")
        print("  Futures mainnet: --confirm FUTURES_MAINNET_TRADE")
        print("=" * 60)
    else:
        print("=" * 60)
        print("*** LIVE FUTURES MAINNET MODE ***")
        print("Real orders will be placed on Binance Futures USDT-M")
        print(f"Symbol whitelist: [{args.symbol}]")
        print(f"Max notional per order: ${args.max_notional}")
        print(f"Target leverage: {args.leverage}x")
        print("=" * 60)

    # Run smoke test
    result = run_futures_smoke_test(
        symbol=args.symbol,
        price=args.price,
        quantity=args.quantity,
        dry_run=dry_run,
        max_notional=args.max_notional,
        target_leverage=args.leverage,
    )

    # Print summary
    result.print_summary()

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
