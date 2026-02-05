"""REST snapshot client for reconciliation.

Periodically polls GET /fapi/v1/openOrders and GET /fapi/v2/positionRisk
to update ObservedStateStore with authoritative exchange state.

See ADR-042 for design decisions.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.execution.binance_port import HttpClient, HttpResponse, map_binance_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from grinder.reconcile.config import ReconcileConfig
    from grinder.reconcile.observed_state import ObservedStateStore

logger = logging.getLogger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY_MS = 1000
DEFAULT_MAX_DELAY_MS = 10000


@dataclass
class SnapshotClientConfig:
    """Configuration for SnapshotClient.

    Attributes:
        base_url: API base URL (default: futures testnet)
        api_key: API key for authentication
        api_secret: API secret for signing
        recv_window_ms: Binance recvWindow parameter (default: 5000)
        timeout_ms: Request timeout (default: 5000)
        max_retries: Maximum retry attempts on transient errors (default: 3)
        base_delay_ms: Initial retry delay (default: 1000)
        max_delay_ms: Maximum retry delay (default: 10000)
    """

    base_url: str = "https://testnet.binancefuture.com"
    api_key: str = ""
    api_secret: str = ""
    recv_window_ms: int = 5000
    timeout_ms: int = 5000

    # Retry configuration
    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay_ms: int = DEFAULT_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS


@dataclass
class SnapshotClient:
    """Client for fetching REST snapshots from Binance Futures.

    Polls /fapi/v1/openOrders and /fapi/v2/positionRisk with retry logic.
    Updates ObservedStateStore with authoritative exchange state.

    Thread-safety: No (use external locking if shared)
    """

    http_client: HttpClient
    config: SnapshotClientConfig
    observed: ObservedStateStore

    _clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    _sleep: Callable[[float], None] = field(default=time.sleep)
    _last_fetch_ts: int = field(default=0, repr=False)

    def _sign_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add timestamp and signature to request params."""
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.config.recv_window_ms

        query_string = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        params["signature"] = signature
        return params

    def _get_headers(self) -> dict[str, str]:
        """Get authenticated request headers."""
        return {"X-MBX-APIKEY": self.config.api_key}

    def _request_with_retry(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
    ) -> HttpResponse:
        """Execute HTTP request with exponential backoff on transient errors.

        Args:
            method: HTTP method
            url: Full URL
            params: Request parameters (will be signed)

        Returns:
            HttpResponse on success

        Raises:
            ConnectorTransientError: After max_retries exhausted
            ConnectorNonRetryableError: On non-retryable errors
        """
        last_error: Exception | None = None
        delay_ms = self.config.base_delay_ms

        for attempt in range(self.config.max_retries + 1):
            try:
                signed_params = self._sign_request(dict(params))

                response = self.http_client.request(
                    method=method,
                    url=url,
                    params=signed_params,
                    headers=self._get_headers(),
                    timeout_ms=self.config.timeout_ms,
                )

                if response.status_code == 200:
                    return response

                # Map error - may raise transient or non-retryable
                map_binance_error(response.status_code, response.json_data)

            except ConnectorTransientError as e:
                last_error = e
                if attempt < self.config.max_retries:
                    logger.warning(
                        "SNAPSHOT_RETRY",
                        extra={
                            "attempt": attempt + 1,
                            "max_retries": self.config.max_retries,
                            "delay_ms": delay_ms,
                            "error": str(e),
                        },
                    )
                    self._sleep(delay_ms / 1000.0)
                    # Exponential backoff with cap
                    delay_ms = min(delay_ms * 2, self.config.max_delay_ms)
                else:
                    raise

            except ConnectorNonRetryableError:
                # Non-retryable errors propagate immediately
                raise

        # Should not reach here, but just in case
        if last_error:
            raise last_error
        raise ConnectorTransientError("Request failed after retries")

    def fetch_open_orders(
        self,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch open orders from exchange.

        Args:
            symbol: Filter by symbol (optional, fetches all if None)

        Returns:
            List of order dicts from Binance API
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol

        url = f"{self.config.base_url}/fapi/v1/openOrders"
        response = self._request_with_retry("GET", url, params)

        if isinstance(response.json_data, list):
            return response.json_data
        return []

    def fetch_positions(
        self,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch position risk from exchange.

        Args:
            symbol: Filter by symbol (optional, fetches all if None)

        Returns:
            List of position dicts from Binance API
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol

        url = f"{self.config.base_url}/fapi/v2/positionRisk"
        response = self._request_with_retry("GET", url, params)

        if isinstance(response.json_data, list):
            return response.json_data
        return []

    def fetch_snapshot(
        self,
        reconcile_config: ReconcileConfig,
    ) -> bool:
        """Fetch full snapshot and update ObservedStateStore.

        Args:
            reconcile_config: Reconciliation configuration (for symbol filter)

        Returns:
            True if snapshot was successful, False on failure
        """
        if not reconcile_config.enabled:
            return False

        now = self._clock()
        symbol_filter = reconcile_config.symbol_filter

        try:
            # Fetch open orders
            orders = self.fetch_open_orders(symbol_filter)
            self.observed.update_from_rest_orders(
                orders,
                ts=now,
                symbol_filter=symbol_filter,
            )

            # Fetch positions
            positions = self.fetch_positions(symbol_filter)
            self.observed.update_from_rest_positions(
                positions,
                ts=now,
                symbol_filter=symbol_filter,
            )

            self._last_fetch_ts = now

            logger.info(
                "SNAPSHOT_SUCCESS",
                extra={
                    "orders_count": len(orders),
                    "positions_count": len(positions),
                    "symbol_filter": symbol_filter,
                    "ts": now,
                },
            )

            return True

        except (ConnectorTransientError, ConnectorNonRetryableError) as e:
            logger.error(
                "SNAPSHOT_FAILED",
                extra={
                    "error": str(e),
                    "symbol_filter": symbol_filter,
                },
            )
            return False

    @property
    def last_fetch_ts(self) -> int:
        """Timestamp of last successful fetch."""
        return self._last_fetch_ts

    def should_fetch(self, reconcile_config: ReconcileConfig) -> bool:
        """Check if enough time has passed for next snapshot.

        Args:
            reconcile_config: Reconciliation configuration

        Returns:
            True if snapshot_interval_sec has elapsed since last fetch
        """
        if not reconcile_config.enabled:
            return False

        now = self._clock()
        elapsed_ms = now - self._last_fetch_ts
        interval_ms = reconcile_config.snapshot_interval_sec * 1000

        return elapsed_ms >= interval_ms
