"""Tests for scripts/http_measured_client.py (Launch-05c).

Covers:
- build_measured_client() disabled (default) returns pass-through
- build_measured_client() enabled returns MeasuredSyncHttpClient with enabled=True
- Invalid env var parsing raises ConfigError
- RequestsHttpClient conforms to HttpClient protocol shape
"""

from __future__ import annotations

import inspect
import os
from unittest.mock import patch

import pytest
from scripts.http_measured_client import (
    ConfigError,
    RequestsHttpClient,
    build_measured_client,
)


class FakeInnerClient:
    """Minimal HttpClient stub for factory tests."""

    def request(
        self,
        method: str = "GET",  # noqa: ARG002
        url: str = "",  # noqa: ARG002
        params: dict[str, str] | None = None,  # noqa: ARG002
        headers: dict[str, str] | None = None,  # noqa: ARG002
        timeout_ms: int = 5000,  # noqa: ARG002
        op: str = "",  # noqa: ARG002
    ) -> object:
        return None


class TestBuildMeasuredClientDisabled:
    """When LATENCY_RETRY_ENABLED is not set or '0', factory returns pass-through."""

    def test_default_env_returns_disabled(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LATENCY_RETRY")}
        with patch.dict(os.environ, env, clear=True):
            client = build_measured_client(FakeInnerClient())
        assert not client._enabled

    def test_explicit_zero_returns_disabled(self) -> None:
        env = {"LATENCY_RETRY_ENABLED": "0"}
        with patch.dict(os.environ, env, clear=True):
            client = build_measured_client(FakeInnerClient())
        assert not client._enabled


class TestBuildMeasuredClientEnabled:
    """When LATENCY_RETRY_ENABLED=1, factory returns enabled wrapper."""

    def test_enabled_returns_enabled_client(self) -> None:
        env = {"LATENCY_RETRY_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=True):
            client = build_measured_client(FakeInnerClient())
        assert client._enabled

    def test_custom_max_attempts(self) -> None:
        env = {
            "LATENCY_RETRY_ENABLED": "1",
            "HTTP_MAX_ATTEMPTS_READ": "3",
            "HTTP_MAX_ATTEMPTS_WRITE": "2",
        }
        with patch.dict(os.environ, env, clear=True):
            client = build_measured_client(FakeInnerClient())
        assert client._enabled
        # max_attempts on retry_policy should be max(3, 2) = 3
        assert client._retry.max_attempts == 3

    def test_deadline_override(self) -> None:
        env = {
            "LATENCY_RETRY_ENABLED": "1",
            "HTTP_DEADLINE_PING_TIME_MS": "500",
        }
        with patch.dict(os.environ, env, clear=True):
            client = build_measured_client(FakeInnerClient())
        assert client._deadline.deadlines["ping_time"] == 500


class TestBuildMeasuredClientBadEnv:
    """Invalid env vars raise ConfigError."""

    def test_invalid_max_attempts_read(self) -> None:
        env = {
            "LATENCY_RETRY_ENABLED": "1",
            "HTTP_MAX_ATTEMPTS_READ": "not_a_number",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ConfigError, match="HTTP_MAX_ATTEMPTS_READ"),
        ):
            build_measured_client(FakeInnerClient())

    def test_invalid_deadline_override(self) -> None:
        env = {
            "LATENCY_RETRY_ENABLED": "1",
            "HTTP_DEADLINE_PING_TIME_MS": "abc",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ConfigError, match="HTTP_DEADLINE_PING_TIME_MS"),
        ):
            build_measured_client(FakeInnerClient())


class TestRequestsHttpClient:
    """RequestsHttpClient has the right interface shape."""

    def test_has_request_method(self) -> None:
        client = RequestsHttpClient()
        assert callable(client.request)

    def test_request_signature_accepts_op(self) -> None:
        """Verify op parameter is accepted (used by MeasuredSyncHttpClient)."""
        sig = inspect.signature(RequestsHttpClient.request)
        assert "op" in sig.parameters
        assert "method" in sig.parameters
        assert "url" in sig.parameters
        assert "timeout_ms" in sig.parameters
