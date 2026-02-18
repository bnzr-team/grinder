#!/usr/bin/env python3
"""Place a micro market order to create a test position for Stage E.

This script places a small market BUY order that fills immediately,
creating a LONG position for testing execute_flatten.

Usage:
    source .env.stage_d
    ALLOW_MAINNET_TRADE=1 PYTHONPATH=src python3 -m scripts.place_test_position

Safety:
    - Micro notional (~$5-10)
    - Single order only
    - Requires explicit ALLOW_MAINNET_TRADE=1

WARNING: This creates a REAL position that costs real money.
         The position will have P&L based on price movement.
         Use Stage E (execute_flatten) to close it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: requests library required")
    sys.exit(1)

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.binance_port import HttpResponse
from grinder.reconcile.identity import OrderIdentityConfig


@dataclass
class RequestsHttpClient:
    """HTTP client using requests library."""

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",  # noqa: ARG002
    ) -> HttpResponse:
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


def get_current_price(http_client: RequestsHttpClient, symbol: str) -> Decimal:
    """Fetch current mark price from Binance."""
    url = f"{BINANCE_FUTURES_MAINNET_URL}/fapi/v1/premiumIndex"
    response = http_client.request(
        method="GET",
        url=url,
        params={"symbol": symbol},
        timeout_ms=5000,
    )
    if response.status_code != 200:
        raise ConnectorNonRetryableError(f"Failed to get price: {response.json_data}")
    if isinstance(response.json_data, dict):
        return Decimal(str(response.json_data.get("markPrice", "0")))
    raise ConnectorNonRetryableError("Unexpected response format")


def main() -> int:  # noqa: PLR0915
    # Check env
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "")

    errors = []
    if not api_key or not api_secret:
        errors.append("BINANCE_API_KEY and BINANCE_API_SECRET required")
    if allow_mainnet != "1":
        errors.append("ALLOW_MAINNET_TRADE=1 required")
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return 1

    # Config
    symbol = "BTCUSDT"
    # Safety cap: maximum notional we're willing to risk
    max_notional_cap = Decimal("20")  # Hard safety cap

    http_client = RequestsHttpClient()

    # Get current price to calculate quantity
    try:
        current_price = get_current_price(http_client, symbol)
    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"ERROR getting price: {e}")
        return 1

    # Calculate minimum possible notional for this symbol
    # Binance BTCUSDT: min qty = 0.001, min notional = $100
    min_qty = Decimal("0.001")
    min_possible_notional = min_qty * current_price
    binance_min_notional = Decimal("100")  # Binance Futures minimum

    print("=" * 60)
    print("POSITION SIZE CHECK")
    print("=" * 60)
    print(f"  Symbol:              {symbol}")
    print(f"  Current Price:       ${current_price:.2f}")
    print(f"  Min Qty (Binance):   {min_qty} BTC")
    print(f"  Min Notional (qty):  ${min_possible_notional:.2f}")
    print(f"  Min Notional (API):  ${binance_min_notional}")
    print(f"  Safety Cap:          ${max_notional_cap}")
    print("=" * 60)

    # P0 SAFETY: Fail if minimum notional exceeds our safety cap
    effective_min = max(min_possible_notional, binance_min_notional)
    if effective_min > max_notional_cap:
        print()
        print("ERROR: Cannot create micro-position on BTCUSDT mainnet.")
        print(f"  Binance minimum notional: ${effective_min:.2f}")
        print(f"  Safety cap:               ${max_notional_cap}")
        print()
        print("Options:")
        print("  1. Use Binance TESTNET for safe testing")
        print("  2. Override with --force-large-position (not implemented)")
        print("  3. Test flatten with existing position")
        print()
        print("max_notional_guard=FAIL")
        return 1

    # Calculate quantity for target notional (just above minimum)
    target_notional = effective_min + Decimal("10")  # $10 buffer
    raw_qty = target_notional / current_price
    quantity = max(Decimal(str(round(float(raw_qty), 3))), min_qty)
    actual_notional = quantity * current_price

    print()
    print("POSITION PARAMETERS:")
    print(f"  Quantity:        {quantity} BTC")
    print(f"  Est. Notional:   ${actual_notional:.2f}")
    print("  max_notional_guard=PASS")
    print()
    print("  WARNING: This is a REAL market order!")
    print("  Use Stage E (execute_flatten) to close this position.")
    print("=" * 60)

    # Confirm
    confirm = input("\nType 'YES' to place order: ")
    if confirm != "YES":
        print("Aborted.")
        return 1

    # Create port with short strategy_id to fit Binance 36-char limit
    identity_config = OrderIdentityConfig(
        prefix="grinder_",
        strategy_id="e",  # Short ID for Stage E
    )

    config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_FUTURES_MAINNET_URL,
        api_key=api_key,
        api_secret=api_secret,
        symbol_whitelist=[symbol],
        dry_run=False,
        allow_mainnet=True,
        max_notional_per_order=max_notional_cap + Decimal("5"),  # Safety cap + buffer
        max_orders_per_run=1,
        max_open_orders=1,
        target_leverage=1,
        identity_config=identity_config,
    )

    port = BinanceFuturesPort(http_client=http_client, config=config)

    try:
        order_id = port.place_market_order(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            reduce_only=False,
        )
        print("\nORDER PLACED (market, should fill immediately):")
        print(f"  clientOrderId: {order_id}")
        print()
        print("Position created. Now run Stage E to flatten it.")
        return 0

    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
