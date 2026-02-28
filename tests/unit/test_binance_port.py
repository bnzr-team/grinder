"""Unit tests for BinanceExchangePort.

Tests cover:
- True dry-run mode: dry_run=True → 0 http_client.request() calls
- Mock transport mode: NoopHttpClient records calls for verification
- SafeMode enforcement: READ_ONLY blocks writes, LIVE_TRADE required
- Mainnet forbidden: api.binance.com rejected in v0.1
- Symbol whitelist: Only allowed symbols can trade
- Error mapping: Binance errors → Connector*Error types
- Place/cancel/replace operations
- Integration with IdempotentExchangePort (H3 idempotency)
- Integration with CircuitBreaker (H4 fast-fail)

See ADR-035 for design decisions.
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
from grinder.risk import KillSwitch, KillSwitchReason

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
        # Higher limit for tests that place multiple orders
        max_orders_per_run=100,
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


@pytest.fixture
def dry_run_config() -> BinanceExchangePortConfig:
    """Create config with dry_run=True (0 http_client calls)."""
    return BinanceExchangePortConfig(
        mode=SafeMode.LIVE_TRADE,
        base_url=BINANCE_SPOT_TESTNET_URL,
        api_key="test_key",
        api_secret="test_secret",
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        dry_run=True,  # ← 0 http_client.request() calls
    )


# --- True Dry-Run Tests (CRITICAL: Proves 0 http_client calls) ---


class TestTrueDryRunMode:
    """Tests proving dry_run=True makes EXACTLY 0 http_client.request() calls.

    This is distinct from NoopHttpClient (mock transport) which still receives calls.
    True dry-run short-circuits BEFORE calling http_client.
    """

    def test_place_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceExchangePortConfig
    ) -> None:
        """place_order with dry_run=True makes 0 http_client calls."""
        port = BinanceExchangePort(http_client=noop_client, config=dry_run_config)

        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            level_id=1,
            ts=1000000,
        )

        # Returns synthetic order_id (v1 format: grinder_{strategy}_{symbol}_{level}_{ts_sec}_{seq})
        assert order_id is not None
        assert "grinder_d_BTCUSDT_1_1000" in order_id

        # CRITICAL: 0 http_client calls
        assert len(noop_client.calls) == 0

    def test_cancel_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceExchangePortConfig
    ) -> None:
        """cancel_order with dry_run=True makes 0 http_client calls."""
        port = BinanceExchangePort(http_client=noop_client, config=dry_run_config)

        order_id = "grinder_BTCUSDT_1_1000000_1"
        result = port.cancel_order(order_id)

        # Returns True (synthetic success)
        assert result is True

        # CRITICAL: 0 http_client calls
        assert len(noop_client.calls) == 0

    def test_replace_order_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceExchangePortConfig
    ) -> None:
        """replace_order with dry_run=True makes 0 http_client calls."""
        port = BinanceExchangePort(http_client=noop_client, config=dry_run_config)

        order_id = "grinder_BTCUSDT_1_1000000_1"
        new_order_id = port.replace_order(
            order_id=order_id,
            new_price=Decimal("51000"),
            new_quantity=Decimal("0.02"),
            ts=2000000,
        )

        # Returns synthetic order_id (v1 format, ts in seconds)
        assert new_order_id is not None
        assert "grinder_d_BTCUSDT_1_2000" in new_order_id

        # CRITICAL: 0 http_client calls (cancel + place both dry-run)
        assert len(noop_client.calls) == 0

    def test_fetch_open_orders_dry_run_zero_http_calls(
        self, noop_client: NoopHttpClient, dry_run_config: BinanceExchangePortConfig
    ) -> None:
        """fetch_open_orders with dry_run=True makes 0 http_client calls."""
        port = BinanceExchangePort(http_client=noop_client, config=dry_run_config)

        orders = port.fetch_open_orders("BTCUSDT")

        # Returns empty list (synthetic)
        assert orders == []

        # CRITICAL: 0 http_client calls
        assert len(noop_client.calls) == 0


# --- Mock Transport Tests (NoopHttpClient records calls) ---


class TestMockTransportMode:
    """Tests for NoopHttpClient mock transport (still calls http_client.request).

    This verifies that WITHOUT dry_run, operations DO call http_client.
    """

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

    def test_place_order_calls_http_client(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """place_order without dry_run DOES call http_client (1 call)."""
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
        # NoopHttpClient recorded exactly 1 call
        assert len(noop_client.calls) == 1
        assert noop_client.calls[0]["method"] == "POST"
        assert "/order" in noop_client.calls[0]["url"]

    def test_cancel_order_calls_http_client(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """cancel_order without dry_run DOES call http_client (1 call)."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)

        order_id = "grinder_BTCUSDT_1_1000000_1"
        result = port.cancel_order(order_id)

        assert result is True  # Mock returns CANCELED
        assert len(noop_client.calls) == 1
        assert noop_client.calls[0]["method"] == "DELETE"

    def test_replace_order_calls_http_client(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """replace_order without dry_run DOES call http_client (2 calls)."""
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

    def test_fetch_open_orders_calls_http_client(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """fetch_open_orders without dry_run DOES call http_client (1 call)."""
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


class TestMainnetGuards:
    """Tests for mainnet safety guards (ADR-039)."""

    def test_mainnet_url_rejected_without_allow_flag(self) -> None:
        """Mainnet URL is rejected without allow_mainnet=True."""
        with pytest.raises(ConnectorNonRetryableError, match="allow_mainnet=True"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com",
            )

    def test_mainnet_partial_url_rejected_without_allow_flag(self) -> None:
        """Any URL containing api.binance.com is rejected without allow_mainnet."""
        with pytest.raises(ConnectorNonRetryableError, match="allow_mainnet=True"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com/api/v3",
            )

    def test_mainnet_rejected_without_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mainnet requires ALLOW_MAINNET_TRADE=1 env var."""
        # Clear the env var
        monkeypatch.delenv("ALLOW_MAINNET_TRADE", raising=False)

        with pytest.raises(ConnectorNonRetryableError, match="ALLOW_MAINNET_TRADE=1"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com",
                allow_mainnet=True,
                symbol_whitelist=["BTCUSDT"],
                max_notional_per_order=Decimal("50"),
            )

    def test_mainnet_rejected_without_symbol_whitelist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mainnet requires non-empty symbol_whitelist."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

        with pytest.raises(ConnectorNonRetryableError, match="symbol_whitelist"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com",
                allow_mainnet=True,
                symbol_whitelist=[],  # Empty!
                max_notional_per_order=Decimal("50"),
            )

    def test_mainnet_rejected_without_max_notional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mainnet requires max_notional_per_order to be set."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

        with pytest.raises(ConnectorNonRetryableError, match="max_notional_per_order"):
            BinanceExchangePortConfig(
                mode=SafeMode.LIVE_TRADE,
                base_url="https://api.binance.com",
                allow_mainnet=True,
                symbol_whitelist=["BTCUSDT"],
                max_notional_per_order=None,  # Not set!
            )

    def test_mainnet_allowed_with_all_guards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mainnet is allowed when all guards are satisfied."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url="https://api.binance.com",
            allow_mainnet=True,
            symbol_whitelist=["BTCUSDT"],
            max_notional_per_order=Decimal("50"),
        )
        assert config.is_mainnet() is True
        assert config.max_notional_per_order == Decimal("50")

    def test_testnet_url_allowed(self) -> None:
        """Testnet URL is allowed without mainnet guards."""
        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url="https://testnet.binance.vision",
        )
        assert "testnet" in config.base_url
        assert config.is_mainnet() is False

    def test_notional_limit_enforced(
        self, noop_client: NoopHttpClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Notional limit blocks orders exceeding max_notional_per_order."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url="https://api.binance.com",
            allow_mainnet=True,
            api_key="test",
            api_secret="test",
            symbol_whitelist=["BTCUSDT"],
            max_notional_per_order=Decimal("50"),  # $50 max
            max_orders_per_run=10,
        )
        port = BinanceExchangePort(http_client=noop_client, config=config)

        # $100 notional exceeds $50 limit
        with pytest.raises(ConnectorNonRetryableError, match="exceeds max_notional"):
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.002"),  # 50000 * 0.002 = $100
                level_id=1,
                ts=1000000,
            )

    def test_order_count_limit_enforced(
        self, noop_client: NoopHttpClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order count limit blocks orders exceeding max_orders_per_run."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url="https://api.binance.com",
            allow_mainnet=True,
            api_key="test",
            api_secret="test",
            symbol_whitelist=["BTCUSDT"],
            max_notional_per_order=Decimal("100"),
            max_orders_per_run=1,  # Only 1 order allowed
        )
        port = BinanceExchangePort(http_client=noop_client, config=config)

        # First order succeeds
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000000,
        )

        # Second order blocked
        with pytest.raises(ConnectorNonRetryableError, match="Order count limit"):
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
                level_id=2,
                ts=1000001,
            )

    def test_reset_clears_order_count(
        self, noop_client: NoopHttpClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reset() clears order count, allowing more orders."""
        monkeypatch.setenv("ALLOW_MAINNET_TRADE", "1")

        config = BinanceExchangePortConfig(
            mode=SafeMode.LIVE_TRADE,
            base_url="https://api.binance.com",
            allow_mainnet=True,
            api_key="test",
            api_secret="test",
            symbol_whitelist=["BTCUSDT"],
            max_notional_per_order=Decimal("100"),
            max_orders_per_run=1,
        )
        port = BinanceExchangePort(http_client=noop_client, config=config)

        # First order
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000000,
        )

        # Reset allows more orders
        port.reset()

        # Now we can place another order
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=2,
            ts=1000001,
        )


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

        # Check the params sent to HTTP client contain our format (v1: grinder_{strategy}_{symbol}_{level}_{ts_sec}_{seq})
        call = noop_client.calls[0]
        client_order_id = call["params"]["newClientOrderId"]
        assert "grinder_d_BTCUSDT_5_1234567" in client_order_id


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


# --- Kill-Switch Integration Tests ---


class TestKillSwitchIntegration:
    """Tests for kill-switch blocking writes.

    IMPORTANT: Kill-switch is NOT built into BinanceExchangePort.
    It's checked at a higher level (e.g., PaperEngine, orchestrator).
    These tests demonstrate the INTEGRATION PATTERN.
    """

    def test_kill_switch_blocks_writes_before_port(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """Demonstrates kill-switch blocking pattern at orchestrator level.

        Pattern:
        1. Check kill_switch.is_triggered BEFORE calling port
        2. If triggered, skip the call entirely (0 HTTP calls)
        """
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)
        kill_switch = KillSwitch()

        # Trip the kill-switch
        kill_switch.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000)
        assert kill_switch.is_triggered is True

        # Orchestrator pattern: check kill-switch BEFORE calling port
        orders_placed = 0
        if not kill_switch.is_triggered:
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),
                level_id=1,
                ts=1000000,
            )
            orders_placed += 1

        # Kill-switch blocked the call
        assert orders_placed == 0

        # CRITICAL: 0 HTTP calls made (blocked before reaching port)
        assert len(noop_client.calls) == 0

    def test_kill_switch_allows_writes_when_not_triggered(
        self, noop_client: NoopHttpClient, live_trade_config: BinanceExchangePortConfig
    ) -> None:
        """When kill-switch is NOT triggered, writes proceed normally."""
        port = BinanceExchangePort(http_client=noop_client, config=live_trade_config)
        kill_switch = KillSwitch()

        # Kill-switch NOT triggered
        assert kill_switch.is_triggered is False

        # Orchestrator pattern: check kill-switch
        if not kill_switch.is_triggered:
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.01"),
                level_id=1,
                ts=1000000,
            )

        # Write succeeded
        assert len(noop_client.calls) == 1

    def test_kill_switch_idempotent_trip(self) -> None:
        """Kill-switch trip is idempotent (second trip is no-op)."""
        kill_switch = KillSwitch()

        # First trip
        state1 = kill_switch.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1000)
        assert state1.triggered is True
        assert state1.triggered_at_ts == 1000

        # Second trip (same reason) is no-op
        state2 = kill_switch.trip(KillSwitchReason.MANUAL, ts=2000)
        assert state2.triggered is True
        assert state2.triggered_at_ts == 1000  # Still first timestamp
        assert state2.reason == KillSwitchReason.DRAWDOWN_LIMIT  # Still first reason
