"""Tests for ListenKeyManager."""

import pytest

from grinder.connectors.binance_user_data_ws import ListenKeyConfig, ListenKeyManager
from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.execution.binance_port import NoopHttpClient


class TestListenKeyManager:
    """Tests for ListenKeyManager HTTP operations."""

    @pytest.fixture
    def noop_client(self) -> NoopHttpClient:
        """Create a NoopHttpClient for testing."""
        return NoopHttpClient()

    @pytest.fixture
    def config(self) -> ListenKeyConfig:
        """Create test config."""
        return ListenKeyConfig(
            base_url="https://testnet.binancefuture.com",
            api_key="test_api_key",
            timeout_ms=5000,
        )

    @pytest.fixture
    def manager(self, noop_client: NoopHttpClient, config: ListenKeyConfig) -> ListenKeyManager:
        """Create ListenKeyManager with test dependencies."""
        return ListenKeyManager(http_client=noop_client, config=config)


class TestCreate(TestListenKeyManager):
    """Tests for listenKey creation."""

    def test_create_returns_listen_key(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return listenKey from response."""
        noop_client.listen_key_response = {"listenKey": "abc123def456"}

        listen_key = manager.create()

        assert listen_key == "abc123def456"

    def test_create_makes_post_request(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should make POST request to correct endpoint."""
        noop_client.listen_key_response = {"listenKey": "abc123"}

        manager.create()

        assert len(noop_client.calls) == 1
        call = noop_client.calls[0]
        assert call["method"] == "POST"
        assert "/fapi/v1/listenKey" in call["url"]

    def test_create_includes_api_key_header(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should include API key in headers."""
        noop_client.listen_key_response = {"listenKey": "abc123"}

        manager.create()

        call = noop_client.calls[0]
        assert call["headers"]["X-MBX-APIKEY"] == "test_api_key"

    def test_create_raises_on_401(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should raise ConnectorNonRetryableError on 401."""
        noop_client.status_code = 401
        noop_client.listen_key_response = {"code": -2015, "msg": "Invalid API-key"}

        with pytest.raises(ConnectorNonRetryableError) as exc_info:
            manager.create()

        assert "Invalid API key" in str(exc_info.value)

    def test_create_raises_on_500(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should raise ConnectorTransientError on 500."""
        noop_client.status_code = 500
        noop_client.listen_key_response = {}

        with pytest.raises(ConnectorTransientError) as exc_info:
            manager.create()

        assert "HTTP 500" in str(exc_info.value)

    def test_create_raises_on_empty_listen_key(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should raise if response has empty listenKey."""
        noop_client.listen_key_response = {"listenKey": ""}

        with pytest.raises(ConnectorTransientError) as exc_info:
            manager.create()

        assert "Empty listenKey" in str(exc_info.value)

    def test_create_raises_on_missing_listen_key(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should raise if response has no listenKey field."""
        noop_client.listen_key_response = {}

        with pytest.raises(ConnectorTransientError) as exc_info:
            manager.create()

        assert "Empty listenKey" in str(exc_info.value)


class TestKeepalive(TestListenKeyManager):
    """Tests for listenKey keepalive."""

    def test_keepalive_returns_true_on_success(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return True on 200 response."""
        noop_client.status_code = 200

        result = manager.keepalive("test_key_123")

        assert result is True

    def test_keepalive_makes_put_request(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should make PUT request to correct endpoint."""
        manager.keepalive("test_key_123")

        assert len(noop_client.calls) == 1
        call = noop_client.calls[0]
        assert call["method"] == "PUT"
        assert "/fapi/v1/listenKey" in call["url"]

    def test_keepalive_includes_listen_key_param(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should include listenKey in params."""
        manager.keepalive("test_key_123")

        call = noop_client.calls[0]
        assert call["params"]["listenKey"] == "test_key_123"

    def test_keepalive_returns_false_on_error(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return False on non-200 response."""
        noop_client.status_code = 400

        result = manager.keepalive("test_key_123")

        assert result is False

    def test_keepalive_handles_exception(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return False on exception."""
        noop_client.raise_exception = Exception("Network error")

        result = manager.keepalive("test_key_123")

        assert result is False


class TestClose(TestListenKeyManager):
    """Tests for listenKey close."""

    def test_close_returns_true_on_success(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return True on 200 response."""
        noop_client.status_code = 200

        result = manager.close("test_key_123")

        assert result is True

    def test_close_makes_delete_request(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should make DELETE request to correct endpoint."""
        manager.close("test_key_123")

        assert len(noop_client.calls) == 1
        call = noop_client.calls[0]
        assert call["method"] == "DELETE"
        assert "/fapi/v1/listenKey" in call["url"]

    def test_close_includes_listen_key_param(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should include listenKey in params."""
        manager.close("test_key_123")

        call = noop_client.calls[0]
        assert call["params"]["listenKey"] == "test_key_123"

    def test_close_returns_false_on_error(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return False on non-200 response."""
        noop_client.status_code = 400

        result = manager.close("test_key_123")

        assert result is False

    def test_close_handles_exception(
        self, manager: ListenKeyManager, noop_client: NoopHttpClient
    ) -> None:
        """Should return False on exception."""
        noop_client.raise_exception = Exception("Network error")

        result = manager.close("test_key_123")

        assert result is False
