"""Unit tests for BinanceExchangePort.

Tests cover:
- Dry-run mode: NoopHttpClient makes 0 real HTTP calls
- SafeMode enforcement: READ_ONLY blocks writes, LIVE_TRADE required
- Mainnet forbidden: api.binance.com rejected in v0.1
- Symbol whitelist: Only allowed symbols can trade
- Error mapping: Binance errors → Connector*Error types
- Place/cancel/replace operations
- Integration with IdempotentExchangePort (H3 idempotency)
- Integration with CircuitBreaker (H4 fast-fail)

See ADR-036 for design decisions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.connectors.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from grinder.connectors.errors import (
    CircuitOpenError,
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.idempotency import InMemoryIdempotencyStore
from grinder.connectors.live_connector import SafeMode
from grinder.core import OrderSide
from grinder.execution import (
    BINANCE_SPOT_TESTNET_URL,
    BinanceExchangePort,
    BinanceExchangePortConfig,
    IdempotentExchangePort,
    NoopHttpClient,
    map_binance_error,
)

# --- Fixtures ---


@pytest.fixture
def noop_client() -> NoopHttpClient:
    """Create a NoopHttpClient for dry-run testing."""
    return NoopHttpClient()


@pytest.fixture
def live_trade_config() -> BinanceExchangePortConfig:
    """Create config with LIVE_TRADE mode (explicit opt-in)."""
    return BinanceExchangePortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_SPOT_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
    )


@pytest.fixture
def read_only_config() -> BinanceExchangePortConfig:
    """Create config with READ_ONLY mode (default, blocks writes)."""
    return BinanceExchangePortConfig(
        mode=SafeMode.READ_ONLY,
        base_url=BINANCE_SPOT_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
    )


@pytest.fixture
def paper_config() -> BinanceExchangePortConfig:
    """Create config with PAPER mode."""
    return BinanceExchangePortConfig(
        mode=SafeMode.PAPER,
        base_url=BINANCE_SPOT_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
    )


# --- Dry-Run Tests (CRITICAL: Proves 0 HTTP calls) ---


class TestDryRunMode:
    """Tests proving NoopHttpClient makes 0 real HTTP calls."""

    def test_noop_client_records_calls(self, noop_client: NoopHttpClient) -> None:
        """NoopHttpClient records calls without making real HTTP."""
        response = noop_client.request(
            method="POST",
            url="https://testnet.binance.vision/api/v3/order",
            params={"symbol": "BTCUSDT"},
        )

        assert response.status_code == 200
        assert len(noop_client.calls) == 1
        assert noop_client.calls[0]["method"] == "POST"
        assert noop_client.calls[0]["url"] == "https://testnet.binance.vision/api/v3/order"

    def test_place_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """place_order with NoopHttpClient makes 0 real HTTP calls."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        # Order placed successfully (mock response)
        assert order_id is not None

        # NoopHttpClient recorded exactly 1 call
        assert len(noop_client.calls) == 1
        assert noop_client.calls[0]["method"] == "POST"
        assert "/order" in noop_client.calls[0]["url"]

        # CRITICAL: No actual network I/O occurred
        # (NoopHttpClient is a pure in-memory mock)

    def test_cancel_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """cancel_order with NoopHttpClient makes 0 real HTTP calls."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        # Use our order ID format: grinder_{symbol}_{level_id}_{ts}_{counter}
        order_id = "grinder_BTCUSDT_1_1000000_1"
        result = port.cancel_order(order_id)

        assert result is True  # Mock returns CANCELED
        assert len(noop_client.calls) == 1
        assert noop_client.calls[0]["method"] == "DELETE"

    def test_replace_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """replace_order with NoopHttpClient makes exactly 2 calls (cancel + place)."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        order_id = "grinder_BTCUSDT_1_1000000_1"
        new_order_id = port.replace_order(
            order_id=order_id,
            new_price=Decimal("51000"),
            new_quantity=Decimal("0.02"),
            ts=2000000,
        )

        assert new_order_id is not None
        # Cancel + Place = 2 calls
        assert len(noop_client.calls) == 2
        assert noop_client.calls[0]["method"] == "DELETE"  # Cancel
        assert noop_client.calls[1]["method"] == "POST"  # Place

    def test_fetch_open_orders_dry_run(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """fetch_open_orders with NoopHttpClient makes 0 real HTTP calls."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        orders = port.fetch_open_orders("BTCUSDT")

        assert orders == []  # Empty mock response
        assert len(noop_client.calls) == 1
        assert noop_client.calls[0]["method"] == "GET"


# --- SafeMode Enforcement Tests ---


class TestSafeModeEnforcement:
    """Tests for SafeMode enforcement."""

    def test_read_only_blocks_place_order(
        self, noop_client: NoopHttpClient, read_only_config: BinanceExchangePortConfig
    ) -> None:
        """READ_ONLY mode blocks place_order."""
        port = BinanceExchangePort(http_client=noop_client, config=read_only_config)

        with pytest.raises(ConnectorNonRetryableError, match="requires LIVE_TRADE"):
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),
                level_id=1,
                ts=1000000,
            )

        # CRITICAL: No HTTP calls were made
        assert len(noop_client.calls) == 0

    def test_read_only_blocks_cancel_order(
        self, noop_client: NoopHttpClient, read_only_config: BinanceExchangePortConfig
    ) -> None:
        """READ_ONLY mode blocks cancel_order."""
        port = BinanceExchangePort(http_client=noop_client, config=read_only_config)

        with pytest.raises(ConnectorNonRetryableError, match="requires LIVE_TRADE"):
            port.cancel_order("grinder_BTCUSDT_1_1000000_1")

        assert len(noop_client.calls) == 0

    def test_read_only_blocks_replace_order(
        self, noop_client: NoopHttpClient, read_only_config: BinanceExchangePortConfig
    ) -> None:
        """READ_ONLY mode blocks replace_order."""
        port = BinanceExchangePort(http_client=noop_client, config=read_only_config)

        with pytest.raises(ConnectorNonRetryableError, match="requires LIVE_TRADE"):
            port.replace_order(
                order_id="grinder_BTCUSDT_1_1000000_1",
                new_price=Decimal("51000"),
                new_quantity=Decimal("0.02"),
                ts=2000000,
            )

        assert len(noop_client.calls) == 0

    def test_paper_mode_blocks_writes(
        self, noop_client: NoopHttpClient, paper_config: BinanceExchangePortConfig
    ) -> None:
        """PAPER mode blocks write operations (only LIVE_TRADE allowed)."""
        port = BinanceExchangePort(http_client=noop_client, config=paper_config)

        with pytest.raises(ConnectorNonRetryableError, match="requires LIVE_TRADE"):
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),
                level_id=1,
                ts=1000000,
            )

        assert len(noop_client.calls) == 0

    def test_live_trade_allows_writes(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """LIVE_TRADE mode allows write operations."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        assert order_id is not None
        assert len(noop_client.calls) == 1


# --- Mainnet Forbidden Tests ---


class TestMainnetForbidden:
    """Tests for mainnet URL rejection in v0.1."""

    def test_mainnet_url_rejected(self) -> None:
        """Mainnet URL is rejected in config."""
        with pytest.raises(ConnectorNonRetryableError, match="Mainnet is forbidden"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com",
            )

    def test_mainnet_partial_url_rejected(self) -> None:
        """Any URL containing api.binance.com is rejected."""
        with pytest.raises(ConnectorNonRetryableError, match=r"Mainnet.*forbidden"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com/api/v3",
            )

    def test_testnet_url_allowed(self) -> None:
        """Testnet URL is allowed."""
        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url="https://testnet.binance.vision",
        )
        assert "testnet" in config.base_url


# --- Symbol Whitelist Tests ---


class TestSymbolWhitelist:
    """Tests for symbol whitelist enforcement."""

    def test_whitelist_allows_listed_symbol(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Symbols in whitelist are allowed."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        # BTCUSDT is in whitelist
        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )
        assert order_id is not None

    def test_whitelist_blocks_unlisted_symbol(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Symbols not in whitelist are blocked."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        with pytest.raises(ConnectorNonRetryableError, match="not in whitelist"):
            port.place_order(
                symbol="DOGEUSDT",  # Not in whitelist
                side=OrderSide.BUY,
                price=Decimal("0.1"),
                quantity=Decimal("100"),
                level_id=1,
                ts=1000000,
            )

        # No HTTP call made
        assert len(noop_client.calls) == 0

    def test_empty_whitelist_allows_all(self, noop_client: NoopHttpClient) -> None:
        """Empty whitelist allows all symbols."""
        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url=BINANCE_SPOT_TESTNET_URL,
            symbol_whitelist=[],  # Empty = all allowed
        )
        port = BinanceExchangePort(http_client=noop_client, config=config)

        order_id = port.place_order(
            symbol="ANYUSDT",
            side=OrderSide.BUY,
            price=Decimal("1"),
            quantity=Decimal("10"),
            level_id=1,
            ts=1000000,
        )
        assert order_id is not None


# --- Error Mapping Tests ---


class TestErrorMapping:
    """Tests for Binance error → Connector*Error mapping."""

    def test_5xx_maps_to_transient(self) -> None:
        """5xx errors map to ConnectorTransientError."""
        with pytest.raises(ConnectorTransientError, match="server error"):
            map_binance_error(500, {"msg": "Internal error"})

    def test_502_maps_to_transient(self) -> None:
        """502 Bad Gateway maps to ConnectorTransientError."""
        with pytest.raises(ConnectorTransientError):
            map_binance_error(502, {})

    def test_429_maps_to_transient(self) -> None:
        """429 Rate Limit maps to ConnectorTransientError."""
        with pytest.raises(ConnectorTransientError, match="rate limit"):
            map_binance_error(429, {"msg": "Too many requests"})

    def test_418_maps_to_non_retryable(self) -> None:
        """418 IP Ban maps to ConnectorNonRetryableError."""
        with pytest.raises(ConnectorNonRetryableError, match="IP banned"):
            map_binance_error(418, {"msg": "IP banned"})

    def test_400_maps_to_non_retryable(self) -> None:
        """400 Bad Request maps to ConnectorNonRetryableError."""
        with pytest.raises(ConnectorNonRetryableError):
            map_binance_error(400, {"code": -1100, "msg": "Bad request"})

    def test_binance_1000_series_maps_to_transient(self) -> None:
        """Binance -1000 series errors map to ConnectorTransientError."""
        with pytest.raises(ConnectorTransientError, match="transient"):
            map_binance_error(400, {"code": -1000, "msg": "WAF limit"})

    def test_binance_1100_series_maps_to_non_retryable(self) -> None:
        """Binance -1100 series errors map to ConnectorNonRetryableError."""
        with pytest.raises(ConnectorNonRetryableError):
            map_binance_error(400, {"code": -1102, "msg": "Invalid param"})


# --- Idempotency Integration Tests (H3) ---


class TestIdempotencyIntegration:
    """Tests for integration with IdempotentExchangePort (H3)."""

    def test_idempotent_port_caches_duplicate_place(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Duplicate place_order returns cached result (H3 idempotency)."""
        raw_port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)
        store = InMemoryIdempotencyStore()
        port = IdempotentExchangePort(inner=raw_port, store=store)

        # First call executes
        order_id_1 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        # Second call with same params returns cached result
        order_id_2 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,  # Same params
        )

        assert order_id_1 == order_id_2

        # Only 1 HTTP call was made (second was cached)
        assert len(noop_client.calls) == 1

        # Stats show 1 executed, 1 cached
        assert port.stats.place_executed == 1
        assert port.stats.place_cached == 1

    def test_idempotent_port_different_params_executes(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Different params create new order (not cached)."""
        raw_port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)
        store = InMemoryIdempotencyStore()
        port = IdempotentExchangePort(inner=raw_port, store=store)

        # First call
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        # Second call with DIFFERENT price
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("51000"),  # Different price
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        # Both calls executed
        assert len(noop_client.calls) == 2
        assert port.stats.place_executed == 2
        assert port.stats.place_cached == 0


# --- Circuit Breaker Integration Tests (H4) ---


class TestCircuitBreakerIntegration:
    """Tests for integration with CircuitBreaker (H4)."""

    def test_circuit_breaker_rejects_when_open(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Circuit breaker rejects calls when OPEN (fast-fail)."""
        raw_port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)
        store = InMemoryIdempotencyStore()
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))

        port = IdempotentExchangePort(inner=raw_port, store=store, breaker=breaker)

        # Trip the breaker manually
        breaker.record_failure("place", "test")
        breaker.record_failure("place", "test")

        # Next call should be rejected
        with pytest.raises(CircuitOpenError, match="place"):
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),
                level_id=1,
                ts=1000000,
            )

        # No HTTP call was made (fast-fail)
        assert len(noop_client.calls) == 0


# --- Order ID Format Tests ---


class TestOrderIdFormat:
    """Tests for deterministic order ID generation."""

    def test_order_id_contains_symbol(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Order ID contains symbol for cancel/replace parsing."""
        # Configure mock to return our client order ID
        noop_client.place_response = {
            "orderId": 12345,
            "clientOrderId": "will_be_overwritten",
            "status": "NEW",
        }

        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=5,
            ts=1234567890,
        )

        # Check the params sent to HTTP client contain our format
        call = noop_client.calls[0]
        client_order_id = call["params"]["newClientOrderId"]
        assert "grinder_BTCUSDT_5_1234567890" in client_order_id


# --- Reset Tests ---


class TestReset:
    """Tests for reset functionality."""

    def test_reset_clears_counter(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """reset() clears internal order counter."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )
        assert port._order_counter == 1

        port.reset()
        assert port._order_counter == 0
