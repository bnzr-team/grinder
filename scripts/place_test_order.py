#!/usr/bin/env python3
"""Place a single test order for Stage D testing.

This script places one limit order far from market to test reconcile cancel.
The order uses grinder_default_... clientOrderId format.

Usage:
    source .env.stage_d
    ALLOW_MAINNET_TRADE=1 PYTHONPATH=src python3 -m scripts.place_test_order

Safety:
    - Far from market price (won't fill)
    - Minimum notional (~$110)
    - Single order only
"""

from __future__ import annotations

import os
import sys
import time
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


def main() -> int:
    # Check env
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_SECRET_KEY", "")
    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "")

    if not api_key or not api_secret:
        print("ERROR: BINANCE_API_KEY and BINANCE_SECRET_KEY required")
        return 1
    if allow_mainnet != "1":
        print("ERROR: ALLOW_MAINNET_TRADE=1 required")
        return 1

    # Config
    symbol = "BTCUSDT"
    # Far from market - BUY at $40,000 when BTC is ~$100k
    price = Decimal("40000.00")
    # Minimum quantity to meet $100 notional requirement
    quantity = Decimal("0.003")  # $120 notional at $40k

    print("=" * 60)
    print("PLACING TEST ORDER FOR STAGE D")
    print("=" * 60)
    print(f"  Symbol:   {symbol}")
    print("  Side:     BUY")
    print(f"  Price:    ${price}")
    print(f"  Quantity: {quantity}")
    print(f"  Notional: ${price * quantity}")
    print()
    print("  ClientOrderId format: grinder_default_BTCUSDT_...")
    print("=" * 60)

    # Create port with identity config
    # Use short strategy_id "d" to keep clientOrderId < 36 chars
    # Format: grinder_d_BTCUSDT_0_<ts_sec>_1 = ~31 chars
    identity_config = OrderIdentityConfig(
        prefix="grinder_",
        strategy_id="d",  # Short ID to meet Binance 36 char limit
    )

    http_client = RequestsHttpClient()
    config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_FUTURES_MAINNET_URL,
        api_key=api_key,
        api_secret=api_secret,
        symbol_whitelist=[symbol],
        dry_run=False,
        allow_mainnet=True,
        max_notional_per_order=Decimal("200"),
        max_orders_per_run=1,
        max_open_orders=1,
        target_leverage=1,
        identity_config=identity_config,
    )

    port = BinanceFuturesPort(http_client=http_client, config=config)

    # Place order with short clientOrderId (Binance limit: 36 chars max)
    # Uses seconds timestamp (not ms) to keep ID short
    ts = int(time.time())
    try:
        order_id = port.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            price=price,
            quantity=quantity,
            level_id=0,  # Short level ID
            ts=ts,  # Seconds instead of ms
            reduce_only=False,
        )
        print("\nORDER PLACED:")
        print(f"  clientOrderId: {order_id}")
        print()
        print("Now run Stage D to cancel this order.")
        return 0

    except (ConnectorNonRetryableError, ConnectorTransientError) as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
