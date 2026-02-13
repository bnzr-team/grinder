#!/usr/bin/env python3
"""Fetch and cache Binance Futures exchangeInfo (M7-06).

Fetches exchangeInfo from Binance Futures REST API and saves to local cache.
Used for populating symbol_constraints (step_size, min_qty) for ExecutionEngine.

Usage:
    # Fetch and save to default cache location
    python -m scripts.fetch_exchange_info

    # Fetch and save to custom location
    python -m scripts.fetch_exchange_info --out var/cache/custom.json

    # Show parsed constraints summary
    python -m scripts.fetch_exchange_info --show

    # Use testnet instead of mainnet
    python -m scripts.fetch_exchange_info --testnet

See: ADR-060 for design decisions
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Any

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from grinder.execution.constraint_provider import (
    BINANCE_FUTURES_EXCHANGE_INFO_URL,
    DEFAULT_CACHE_DIR,
    DEFAULT_CACHE_FILE,
    parse_exchange_info,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

BINANCE_FUTURES_TESTNET_URL = "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"


def fetch_exchange_info(url: str, timeout: int = 30) -> dict[str, Any]:
    """Fetch exchangeInfo from Binance API.

    Args:
        url: API endpoint URL
        timeout: Request timeout in seconds

    Returns:
        Raw exchangeInfo JSON response
    """
    logger.info("Fetching exchangeInfo from: %s", url)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "grinder/1.0"},
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"API returned status {response.status}")
        data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return data


def save_to_file(data: dict[str, Any], path: Path) -> None:
    """Save exchangeInfo to JSON file.

    Args:
        data: Raw exchangeInfo response
        path: Output file path
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    logger.info("Saved to: %s", path)


def show_constraints_summary(data: dict[str, Any], limit: int = 20) -> None:
    """Print summary of parsed constraints.

    Args:
        data: Raw exchangeInfo response
        limit: Max symbols to show (0 = all)
    """
    constraints = parse_exchange_info(data)

    logger.info("\n--- Symbol Constraints Summary ---")
    logger.info("Total symbols: %d", len(constraints))
    logger.info("")
    logger.info("%-12s  %-12s  %-12s", "Symbol", "step_size", "min_qty")
    logger.info("-" * 40)

    for shown, (symbol, c) in enumerate(sorted(constraints.items()), start=1):
        logger.info("%-12s  %-12s  %-12s", symbol, c.step_size, c.min_qty)
        if limit > 0 and shown >= limit:
            remaining = len(constraints) - shown
            if remaining > 0:
                logger.info("... and %d more symbols", remaining)
            break


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Fetch and cache Binance Futures exchangeInfo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_CACHE_DIR / DEFAULT_CACHE_FILE,
        help=f"Output file path (default: {DEFAULT_CACHE_DIR / DEFAULT_CACHE_FILE})",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Binance Futures testnet instead of mainnet",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show parsed constraints summary",
    )
    parser.add_argument(
        "--show-limit",
        type=int,
        default=20,
        help="Max symbols to show in summary (default: 20, 0 = all)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Request timeout in seconds (default: 30)",
    )

    args = parser.parse_args()

    url = BINANCE_FUTURES_TESTNET_URL if args.testnet else BINANCE_FUTURES_EXCHANGE_INFO_URL

    try:
        data = fetch_exchange_info(url, timeout=args.timeout)

        # Validate by parsing
        constraints = parse_exchange_info(data)
        logger.info("Parsed constraints for %d symbols", len(constraints))

        # Save to file
        save_to_file(data, args.out)

        # Optionally show summary
        if args.show:
            show_constraints_summary(data, limit=args.show_limit)

        logger.info("\nDone. Use in code:")
        logger.info(
            "  from grinder.execution.constraint_provider import load_constraints_from_file"
        )
        logger.info("  constraints = load_constraints_from_file(Path('%s'))", args.out)

        return 0

    except Exception as e:
        logger.error("Error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
