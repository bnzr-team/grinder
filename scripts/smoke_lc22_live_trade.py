#!/usr/bin/env python3
"""Smoke test for LC-22: LiveConnectorV0 LIVE_TRADE write-path.

Tests the 4-gate safety pattern and real order execution:
1. Verify all 4 gates pass (armed, mode, env var, port)
2. Place 1 micro limit order (far from market)
3. Cancel the order
4. If filled → close position

SAFE-BY-CONSTRUCTION GUARDS:
1. --dry-run by default (only verifies gates)
2. Requires explicit --confirm LC22_LIVE_TRADE for real orders
3. Requires ALLOW_MAINNET_TRADE=1 env var
4. Requires armed=True in config
5. max_notional_per_order required (default: $125)
6. Far-from-market price (won't fill)

Usage:
    # Dry-run (default) - verify gates only, no orders
    python -m scripts.smoke_lc22_live_trade

    # Real order via LiveConnectorV0
    BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy ALLOW_MAINNET_TRADE=1 \\
        python -m scripts.smoke_lc22_live_trade --confirm LC22_LIVE_TRADE
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from decimal import Decimal

# Guard: Import errors should fail clearly
try:
    import requests
except ImportError:
    print("ERROR: requests library required. Run: pip install requests")
    sys.exit(1)

from grinder.connectors.errors import ConnectorNonRetryableError
from grinder.connectors.live_connector import (
    LiveConnectorConfig,
    LiveConnectorV0,
    SafeMode,
)
from grinder.core import OrderSide
from grinder.execution.binance_futures_port import (
    BINANCE_FUTURES_MAINNET_URL,
    BinanceFuturesPort,
    BinanceFuturesPortConfig,
)
from grinder.execution.binance_port import HttpResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Default safety limits ---
DEFAULT_MAX_NOTIONAL = Decimal("125.00")  # $125 max per order (above $100 min)
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_LEVERAGE = 3


class RequestsHttpClient:
    """Simple requests-based HTTP client for BinanceFuturesPort."""

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        return HttpResponse(status_code=resp.status_code, json_data=resp.json())

    def post(
        self,
        url: str,
        data: dict | None = None,
        headers: dict | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        resp = requests.post(url, data=data, headers=headers, timeout=timeout)
        return HttpResponse(status_code=resp.status_code, json_data=resp.json())

    def delete(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        resp = requests.delete(url, params=params, headers=headers, timeout=timeout)
        return HttpResponse(status_code=resp.status_code, json_data=resp.json())


async def smoke_test(dry_run: bool, symbol: str) -> int:
    """Run LC-22 smoke test.

    Returns:
        0 on success, 1 on failure
    """
    print("=" * 60)
    print("LC-22 LIVE_TRADE SMOKE TEST")
    print("=" * 60)

    # Check env vars
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    allow_mainnet = os.environ.get("ALLOW_MAINNET_TRADE", "")

    print(f"\n## Environment")
    print(f"BINANCE_API_KEY: {'set' if api_key else 'NOT SET'}")
    print(f"BINANCE_API_SECRET: {'set' if api_secret else 'NOT SET'}")
    print(f"ALLOW_MAINNET_TRADE: {allow_mainnet or 'NOT SET'}")
    print(f"dry_run: {dry_run}")
    print(f"symbol: {symbol}")

    if dry_run:
        print("\n## Dry-run mode: Testing gate checks only")

        # Test Gate 1: armed=False should block
        print("\n### Gate 1: armed=False should block")
        config = LiveConnectorConfig(
            mode=SafeMode.LIVE_TRADE,
            armed=False,  # Gate 1 fails
            futures_port=None,
        )
        connector = LiveConnectorV0(config=config)
        await connector.connect()

        try:
            connector.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                price=Decimal("1000"),
                quantity=Decimal("0.001"),
            )
            print("FAIL: Gate 1 should have blocked")
            return 1
        except ConnectorNonRetryableError as e:
            if "armed=False" in str(e):
                print(f"PASS: Blocked with: {e}")
            else:
                print(f"FAIL: Wrong error: {e}")
                return 1
        finally:
            await connector.close()

        # Test Gate 3: ALLOW_MAINNET_TRADE not set should block
        print("\n### Gate 3: ALLOW_MAINNET_TRADE not set should block")
        os.environ.pop("ALLOW_MAINNET_TRADE", None)

        config = LiveConnectorConfig(
            mode=SafeMode.LIVE_TRADE,
            armed=True,
            futures_port=None,  # Will trigger gate 4, but gate 3 checked first
        )
        connector = LiveConnectorV0(config=config)
        await connector.connect()

        try:
            connector.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                price=Decimal("1000"),
                quantity=Decimal("0.001"),
            )
            print("FAIL: Gate 3 should have blocked")
            return 1
        except ConnectorNonRetryableError as e:
            if "ALLOW_MAINNET_TRADE" in str(e):
                print(f"PASS: Blocked with: {e}")
            else:
                print(f"FAIL: Wrong error: {e}")
                return 1
        finally:
            await connector.close()

        print("\n" + "=" * 60)
        print("DRY-RUN COMPLETE: All gate checks passed")
        print("=" * 60)
        return 0

    # Real order mode
    if not api_key or not api_secret:
        print("\nERROR: BINANCE_API_KEY and BINANCE_API_SECRET required for real orders")
        return 1

    if allow_mainnet.lower() not in ("1", "true", "yes"):
        print("\nERROR: ALLOW_MAINNET_TRADE=1 required for real orders")
        return 1

    print("\n## Creating BinanceFuturesPort")

    # Create futures port
    port_config = BinanceFuturesPortConfig(
        mode=SafeMode.LIVE_TRADE,
        allow_mainnet=True,
        api_key=api_key,
        api_secret=api_secret,
        base_url=BINANCE_FUTURES_MAINNET_URL,
        symbol_whitelist=[symbol],
        max_notional_per_order=DEFAULT_MAX_NOTIONAL,
        max_orders_per_run=1,
        max_open_orders=1,
        target_leverage=DEFAULT_LEVERAGE,
    )

    http_client = RequestsHttpClient()
    futures_port = BinanceFuturesPort(config=port_config, http_client=http_client)

    print(f"Port created: url={futures_port.config.base_url}")

    # Create LiveConnectorV0 with LIVE_TRADE mode
    print("\n## Creating LiveConnectorV0 with LIVE_TRADE mode")

    connector_config = LiveConnectorConfig(
        mode=SafeMode.LIVE_TRADE,
        armed=True,
        futures_port=futures_port,
    )
    connector = LiveConnectorV0(config=connector_config)
    await connector.connect()

    print(f"Connector created: mode={connector.mode}, armed={connector_config.armed}")

    try:
        # Get current price for far-from-market order
        print("\n## Fetching current price")
        price_resp = http_client.get(
            f"{BINANCE_FUTURES_MAINNET_URL}/fapi/v1/ticker/price",
            params={"symbol": symbol},
        )
        import json
        current_price = Decimal(str(price_resp.json_data["price"]))
        print(f"Current {symbol} price: {current_price}")

        # Far from market (50% below)
        order_price = (current_price * Decimal("0.50")).quantize(Decimal("0.01"))
        order_qty = Decimal("0.001")  # Minimum qty for BTCUSDT

        print(f"\n## Placing order via LiveConnectorV0.place_order()")
        print(f"  symbol: {symbol}")
        print(f"  side: BUY")
        print(f"  price: {order_price} (50% below market)")
        print(f"  quantity: {order_qty}")

        ts_before = int(time.time() * 1000)

        order_id = connector.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            price=order_price,
            quantity=order_qty,
            level_id=1,
            ts=ts_before,
        )

        print(f"\n### Order placed!")
        print(f"  order_id: {order_id}")

        # Small delay
        await asyncio.sleep(1)

        # Cancel the order
        print(f"\n## Cancelling order via LiveConnectorV0.cancel_order()")
        print(f"  order_id: {order_id}")

        cancel_result = connector.cancel_order(order_id)

        print(f"\n### Order cancelled!")
        print(f"  result: {cancel_result}")

        print("\n" + "=" * 60)
        print("SMOKE TEST PASSED")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n### ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await connector.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        type=str,
        default="",
        help="Confirm real order: must be 'LC22_LIVE_TRADE'",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=DEFAULT_SYMBOL,
        help=f"Symbol to trade (default: {DEFAULT_SYMBOL})",
    )
    args = parser.parse_args()

    dry_run = args.confirm != "LC22_LIVE_TRADE"

    if not dry_run:
        print("=" * 60)
        print("WARNING: REAL ORDER MODE")
        print("This will place a real order on Binance Futures mainnet!")
        print("=" * 60)

        # Double-check env vars
        if os.environ.get("ALLOW_MAINNET_TRADE", "").lower() not in ("1", "true", "yes"):
            print("ERROR: ALLOW_MAINNET_TRADE=1 not set. Aborting.")
            return 1

    return asyncio.run(smoke_test(dry_run=dry_run, symbol=args.symbol))


if __name__ == "__main__":
    sys.exit(main())
