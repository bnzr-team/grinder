"""Tests for SnapshotClient."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.execution.binance_port import HttpResponse
from grinder.reconcile.config import ReconcileConfig
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.snapshot_client import SnapshotClient, SnapshotClientConfig


@dataclass
class MockHttpClient:
    """Mock HTTP client for testing."""

    responses: list[HttpResponse] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _call_index: int = field(default=0, repr=False)

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
    ) -> HttpResponse:
        """Return next response in sequence."""
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "timeout_ms": timeout_ms,
            }
        )

        if self._call_index < len(self.responses):
            response = self.responses[self._call_index]
            self._call_index += 1
            return response

        # Default success response
        return HttpResponse(status_code=200, json_data=[])


class TestSnapshotClientFetchOrders:
    """Tests for open orders fetching."""

    def test_fetch_open_orders_success(self) -> None:
        """Test successful open orders fetch."""
        orders = [
            {
                "orderId": 12345678,
                "symbol": "BTCUSDT",
                "clientOrderId": "grinder_BTCUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "42500.00",
                "origQty": "0.010",
                "executedQty": "0",
                "avgPrice": "0",
            }
        ]

        client = MockHttpClient(responses=[HttpResponse(status_code=200, json_data=orders)])
        config = SnapshotClientConfig()
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
        )

        result = snapshot.fetch_open_orders()

        assert len(result) == 1
        assert result[0]["orderId"] == 12345678
        assert "/fapi/v1/openOrders" in client.calls[0]["url"]

    def test_fetch_open_orders_with_symbol_filter(self) -> None:
        """Test open orders fetch with symbol filter."""
        client = MockHttpClient(responses=[HttpResponse(status_code=200, json_data=[])])
        config = SnapshotClientConfig()
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
        )

        snapshot.fetch_open_orders(symbol="BTCUSDT")

        assert client.calls[0]["params"]["symbol"] == "BTCUSDT"


class TestSnapshotClientFetchPositions:
    """Tests for position risk fetching."""

    def test_fetch_positions_success(self) -> None:
        """Test successful positions fetch."""
        positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "42500.00",
                "unRealizedProfit": "50.00",
            }
        ]

        client = MockHttpClient(responses=[HttpResponse(status_code=200, json_data=positions)])
        config = SnapshotClientConfig()
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
        )

        result = snapshot.fetch_positions()

        assert len(result) == 1
        assert result[0]["symbol"] == "BTCUSDT"
        assert "/fapi/v2/positionRisk" in client.calls[0]["url"]


class TestSnapshotClientRetry:
    """Tests for retry logic."""

    def test_retry_on_429(self) -> None:
        """Test retry on rate limit (429)."""
        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        # First call returns 429, second succeeds
        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=429, json_data={"msg": "Rate limit"}),
                HttpResponse(status_code=200, json_data=[]),
            ]
        )
        config = SnapshotClientConfig(
            max_retries=3,
            base_delay_ms=100,
        )
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _sleep=mock_sleep,
        )

        result = snapshot.fetch_open_orders()

        assert len(result) == 0  # Empty success response
        assert len(client.calls) == 2  # 2 calls total
        assert len(sleep_calls) == 1  # 1 retry sleep
        assert sleep_calls[0] == 0.1  # 100ms

    def test_retry_on_5xx(self) -> None:
        """Test retry on server error (5xx)."""
        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        # First two calls return 500, third succeeds
        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=500, json_data={"msg": "Server error"}),
                HttpResponse(status_code=503, json_data={"msg": "Unavailable"}),
                HttpResponse(status_code=200, json_data=[]),
            ]
        )
        config = SnapshotClientConfig(
            max_retries=3,
            base_delay_ms=100,
            max_delay_ms=500,
        )
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _sleep=mock_sleep,
        )

        result = snapshot.fetch_open_orders()

        assert len(result) == 0
        assert len(client.calls) == 3
        assert len(sleep_calls) == 2
        # Exponential backoff: 100ms, 200ms
        assert sleep_calls[0] == 0.1
        assert sleep_calls[1] == 0.2

    def test_exponential_backoff_capped(self) -> None:
        """Test exponential backoff is capped at max_delay_ms."""
        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        # All calls fail except last
        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=200, json_data=[]),
            ]
        )
        config = SnapshotClientConfig(
            max_retries=3,
            base_delay_ms=100,
            max_delay_ms=150,  # Cap at 150ms
        )
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _sleep=mock_sleep,
        )

        snapshot.fetch_open_orders()

        # 100ms, 150ms (capped), 150ms (capped)
        assert sleep_calls[0] == 0.1
        assert sleep_calls[1] == 0.15  # Capped
        assert sleep_calls[2] == 0.15  # Still capped

    def test_max_retries_exceeded_raises(self) -> None:
        """Test that exceeding max retries raises error."""
        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        # All calls fail
        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
            ]
        )
        config = SnapshotClientConfig(max_retries=2)
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _sleep=mock_sleep,
        )

        with pytest.raises(ConnectorTransientError):
            snapshot.fetch_open_orders()

        # Initial + 2 retries = 3 calls
        assert len(client.calls) == 3

    def test_no_retry_on_4xx(self) -> None:
        """Test that 4xx errors (except 429) don't retry."""
        sleep_calls: list[float] = []

        def mock_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=400, json_data={"code": -1100, "msg": "Bad request"}),
            ]
        )
        config = SnapshotClientConfig(max_retries=3)
        observed = ObservedStateStore()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _sleep=mock_sleep,
        )

        with pytest.raises(ConnectorNonRetryableError):
            snapshot.fetch_open_orders()

        assert len(client.calls) == 1  # No retries
        assert len(sleep_calls) == 0


class TestSnapshotClientFetchSnapshot:
    """Tests for full snapshot fetching."""

    def test_fetch_snapshot_updates_observed_store(self) -> None:
        """Test fetch_snapshot updates ObservedStateStore."""
        orders = [
            {
                "orderId": 12345678,
                "symbol": "BTCUSDT",
                "clientOrderId": "grinder_BTCUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "42500.00",
                "origQty": "0.010",
                "executedQty": "0",
                "avgPrice": "0",
            }
        ]
        positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "42500.00",
                "unRealizedProfit": "50.00",
            }
        ]

        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=200, json_data=orders),
                HttpResponse(status_code=200, json_data=positions),
            ]
        )
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _clock=lambda: 2000000,
        )

        result = snapshot.fetch_snapshot(reconcile_config)

        assert result is True
        assert observed.get_order("grinder_BTCUSDT_1_1000000_1") is not None
        assert observed.get_position("BTCUSDT") is not None
        assert snapshot.last_fetch_ts == 2000000

    def test_fetch_snapshot_with_symbol_filter(self) -> None:
        """Test fetch_snapshot respects symbol filter."""
        orders = [
            {
                "orderId": 1,
                "symbol": "BTCUSDT",
                "clientOrderId": "grinder_BTCUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "42500.00",
                "origQty": "0.010",
                "executedQty": "0",
                "avgPrice": "0",
            },
            {
                "orderId": 2,
                "symbol": "ETHUSDT",
                "clientOrderId": "grinder_ETHUSDT_1_1000000_1",
                "side": "BUY",
                "status": "NEW",
                "price": "3000.00",
                "origQty": "0.1",
                "executedQty": "0",
                "avgPrice": "0",
            },
        ]
        positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "42500.00",
                "unRealizedProfit": "50.00",
            },
            {
                "symbol": "ETHUSDT",
                "positionAmt": "0.5",
                "entryPrice": "3000.00",
                "unRealizedProfit": "10.00",
            },
        ]

        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=200, json_data=orders),
                HttpResponse(status_code=200, json_data=positions),
            ]
        )
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig(symbol_filter="BTCUSDT")

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
        )

        snapshot.fetch_snapshot(reconcile_config)

        # Only BTCUSDT should be in store
        assert observed.get_order("grinder_BTCUSDT_1_1000000_1") is not None
        assert observed.get_order("grinder_ETHUSDT_1_1000000_1") is None
        assert observed.get_position("BTCUSDT") is not None
        assert observed.get_position("ETHUSDT") is None

    def test_fetch_snapshot_disabled_returns_false(self) -> None:
        """Test fetch_snapshot returns False when disabled."""
        client = MockHttpClient()
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig(enabled=False)

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
        )

        result = snapshot.fetch_snapshot(reconcile_config)

        assert result is False
        assert len(client.calls) == 0

    def test_fetch_snapshot_failure_returns_false(self) -> None:
        """Test fetch_snapshot returns False on error."""
        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
                HttpResponse(status_code=500, json_data={}),
            ]
        )
        config = SnapshotClientConfig(max_retries=2)
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig()

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _sleep=lambda _: None,
        )

        result = snapshot.fetch_snapshot(reconcile_config)

        assert result is False


class TestSnapshotClientShouldFetch:
    """Tests for should_fetch timing."""

    def test_should_fetch_returns_true_initially(self) -> None:
        """Test should_fetch returns True when never fetched."""
        client = MockHttpClient()
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig(snapshot_interval_sec=60)

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _clock=lambda: 1000000,
        )

        assert snapshot.should_fetch(reconcile_config) is True

    def test_should_fetch_returns_false_within_interval(self) -> None:
        """Test should_fetch returns False within interval."""
        orders: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []

        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=200, json_data=orders),
                HttpResponse(status_code=200, json_data=positions),
            ]
        )
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig(snapshot_interval_sec=60)

        now = 1000000

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _clock=lambda: now,
        )

        # First fetch
        snapshot.fetch_snapshot(reconcile_config)

        # Check shortly after (30s later)
        snapshot._clock = lambda: now + 30000

        assert snapshot.should_fetch(reconcile_config) is False

    def test_should_fetch_returns_true_after_interval(self) -> None:
        """Test should_fetch returns True after interval elapsed."""
        orders: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []

        client = MockHttpClient(
            responses=[
                HttpResponse(status_code=200, json_data=orders),
                HttpResponse(status_code=200, json_data=positions),
            ]
        )
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig(snapshot_interval_sec=60)

        now = 1000000

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
            _clock=lambda: now,
        )

        # First fetch
        snapshot.fetch_snapshot(reconcile_config)

        # Check after interval (61s later)
        snapshot._clock = lambda: now + 61000

        assert snapshot.should_fetch(reconcile_config) is True

    def test_should_fetch_returns_false_when_disabled(self) -> None:
        """Test should_fetch returns False when reconcile disabled."""
        client = MockHttpClient()
        config = SnapshotClientConfig()
        observed = ObservedStateStore()
        reconcile_config = ReconcileConfig(enabled=False)

        snapshot = SnapshotClient(
            http_client=client,
            config=config,
            observed=observed,
        )

        assert snapshot.should_fetch(reconcile_config) is False
