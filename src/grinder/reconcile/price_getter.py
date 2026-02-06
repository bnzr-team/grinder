"""Price getter for reconciliation (LC-14b).

Provides current market price for flatten notional calculation.

Uses Binance Futures REST API:
- GET /fapi/v1/ticker/price for simple last price
- GET /fapi/v2/ticker/price for USDT-M futures (preferred)

Safety:
- Read-only (no trading actions)
- Timeout protection
- Returns None if price unavailable (caller handles fallback)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.execution.binance_port import HttpClient

logger = logging.getLogger(__name__)

# Default endpoints
DEFAULT_BASE_URL = "https://fapi.binance.com"  # Production futures API
TESTNET_BASE_URL = "https://testnet.binancefuture.com"

# Default timeout for price fetch
DEFAULT_TIMEOUT_MS = 5000


@dataclass
class PriceGetterConfig:
    """Configuration for PriceGetter.

    Attributes:
        base_url: Binance Futures API base URL
        timeout_ms: Request timeout in milliseconds
    """

    base_url: str = DEFAULT_BASE_URL
    timeout_ms: int = DEFAULT_TIMEOUT_MS


@dataclass
class PriceGetter:
    """Fetches current market price from Binance Futures REST API.

    Thread-safety: Not thread-safe (use separate instances per thread)

    Usage:
        getter = PriceGetter(http_client=client, config=config)
        price = getter.get_price("BTCUSDT")  # Returns Decimal or None

        # As a callable for ReconcileRunner:
        runner = ReconcileRunner(..., price_getter=getter.get_price)
    """

    http_client: HttpClient
    config: PriceGetterConfig = field(default_factory=PriceGetterConfig)

    # Cache: symbol → (price, timestamp_ms)
    _cache: dict[str, tuple[Decimal, int]] = field(default_factory=dict, repr=False)
    _cache_ttl_ms: int = 1000  # 1 second cache

    def get_price(self, symbol: str) -> Decimal | None:
        """Get current price for symbol.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")

        Returns:
            Current price as Decimal, or None if unavailable
        """
        try:
            # Check cache first
            cached = self._get_cached(symbol)
            if cached is not None:
                return cached

            # Fetch from REST
            url = f"{self.config.base_url}/fapi/v1/ticker/price"
            params = {"symbol": symbol}

            response = self.http_client.request(
                "GET",
                url,
                params=params,
                timeout_ms=self.config.timeout_ms,
            )

            if response.status_code != 200:
                logger.warning(
                    "PRICE_GETTER_ERROR",
                    extra={
                        "symbol": symbol,
                        "status_code": response.status_code,
                        "reason": "non-200 response",
                    },
                )
                return None

            data = response.json_data
            if not isinstance(data, dict):
                logger.warning(
                    "PRICE_GETTER_ERROR",
                    extra={"symbol": symbol, "reason": "unexpected response format"},
                )
                return None
            price = Decimal(data["price"])

            # Update cache
            self._update_cache(symbol, price)

            logger.debug(
                "PRICE_GETTER_FETCH",
                extra={"symbol": symbol, "price": str(price)},
            )

            return price

        except Exception:
            logger.exception(
                "PRICE_GETTER_EXCEPTION",
                extra={"symbol": symbol},
            )
            return None

    def _get_cached(self, symbol: str) -> Decimal | None:
        """Get cached price if still valid."""
        import time  # noqa: PLC0415

        if symbol not in self._cache:
            return None

        price, cached_ts = self._cache[symbol]
        now_ms = int(time.time() * 1000)

        if now_ms - cached_ts > self._cache_ttl_ms:
            return None

        return price

    def _update_cache(self, symbol: str, price: Decimal) -> None:
        """Update price cache."""
        import time  # noqa: PLC0415

        now_ms = int(time.time() * 1000)
        self._cache[symbol] = (price, now_ms)

    def clear_cache(self) -> None:
        """Clear price cache."""
        self._cache.clear()


def create_price_getter(
    http_client: HttpClient,
    *,
    testnet: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> PriceGetter:
    """Factory to create PriceGetter.

    Args:
        http_client: HTTP client for REST calls
        testnet: If True, use testnet URL
        timeout_ms: Request timeout

    Returns:
        Configured PriceGetter instance
    """
    base_url = TESTNET_BASE_URL if testnet else DEFAULT_BASE_URL
    config = PriceGetterConfig(base_url=base_url, timeout_ms=timeout_ms)
    return PriceGetter(http_client=http_client, config=config)
