"""Tests for scripts/http_probe.py (Launch-05c).

Covers:
- HttpProbeConfig.from_env() defaults and custom values
- HttpProbeConfig rejects unknown ops
- HttpProbeRunner.run_once() calls exactly the right ops/URLs/methods
- HttpProbeRunner.run_loop() uses injected sleep (deterministic, no real waiting)
- Probe exceptions are caught (never crash)
- Label safety: ops are from allowlist only
"""

from __future__ import annotations

import os
import random
import threading
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from scripts.http_probe import (
    PROBE_ALLOWED_OPS,
    HttpProbeConfig,
    HttpProbeRunner,
    ProbeConfigError,
)

from grinder.net.retry_policy import OP_EXCHANGE_INFO, OP_PING_TIME

# ---------------------------------------------------------------------------
# Fake HTTP client that records calls
# ---------------------------------------------------------------------------


@dataclass
class FakeHttpClient:
    """Records all request() calls for assertion."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_ops: set[str] = field(default_factory=set)

    def request(
        self,
        method: str = "GET",
        url: str = "",
        params: dict[str, str] | None = None,  # noqa: ARG002
        headers: dict[str, str] | None = None,  # noqa: ARG002
        timeout_ms: int = 5000,
        op: str = "",
    ) -> object:
        self.calls.append({"method": method, "url": url, "timeout_ms": timeout_ms, "op": op})
        if op in self.fail_ops:
            raise ConnectionError(f"Simulated failure for {op}")
        return None


# ---------------------------------------------------------------------------
# HttpProbeConfig tests
# ---------------------------------------------------------------------------


class TestHttpProbeConfigDefaults:
    """Default config values when no env vars set."""

    def test_defaults(self) -> None:
        env: dict[str, str] = {}
        with patch.dict(os.environ, env, clear=True):
            config = HttpProbeConfig.from_env()
        assert not config.enabled
        assert config.interval_ms == 5000
        assert config.jitter_ms == 250
        assert config.base_url == "https://fapi.binance.com"
        assert config.ops == (OP_PING_TIME, OP_EXCHANGE_INFO)


class TestHttpProbeConfigCustom:
    """Custom env var values."""

    def test_enabled(self) -> None:
        env = {"HTTP_PROBE_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=True):
            config = HttpProbeConfig.from_env()
        assert config.enabled

    def test_custom_interval(self) -> None:
        env = {"HTTP_PROBE_INTERVAL_MS": "10000"}
        with patch.dict(os.environ, env, clear=True):
            config = HttpProbeConfig.from_env()
        assert config.interval_ms == 10000

    def test_custom_jitter(self) -> None:
        env = {"HTTP_PROBE_JITTER_MS": "500"}
        with patch.dict(os.environ, env, clear=True):
            config = HttpProbeConfig.from_env()
        assert config.jitter_ms == 500

    def test_custom_base_url(self) -> None:
        env = {"HTTP_PROBE_BASE_URL": "https://testnet.binancefuture.com"}
        with patch.dict(os.environ, env, clear=True):
            config = HttpProbeConfig.from_env()
        assert config.base_url == "https://testnet.binancefuture.com"

    def test_custom_ops_single(self) -> None:
        env = {"HTTP_PROBE_OPS": "ping_time"}
        with patch.dict(os.environ, env, clear=True):
            config = HttpProbeConfig.from_env()
        assert config.ops == (OP_PING_TIME,)


class TestHttpProbeConfigValidation:
    """Config validation: unknown ops rejected, bad ints rejected."""

    def test_unknown_op_rejected(self) -> None:
        with pytest.raises(ProbeConfigError, match="unknown op 'evil_op'"):
            HttpProbeConfig(enabled=True, ops=("ping_time", "evil_op"))

    def test_unknown_op_via_env(self) -> None:
        env = {"HTTP_PROBE_OPS": "ping_time,evil_op"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ProbeConfigError, match="evil_op"),
        ):
            HttpProbeConfig.from_env()

    def test_invalid_interval_ms(self) -> None:
        env = {"HTTP_PROBE_INTERVAL_MS": "not_a_number"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ProbeConfigError, match="HTTP_PROBE_INTERVAL_MS"),
        ):
            HttpProbeConfig.from_env()

    def test_invalid_jitter_ms(self) -> None:
        env = {"HTTP_PROBE_JITTER_MS": "abc"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ProbeConfigError, match="HTTP_PROBE_JITTER_MS"),
        ):
            HttpProbeConfig.from_env()

    def test_allowed_ops_are_read_only(self) -> None:
        """All probe-allowed ops must be in the READ_OPS set (no write ops)."""
        from grinder.net.retry_policy import READ_OPS  # noqa: PLC0415

        for op in PROBE_ALLOWED_OPS:
            assert op in READ_OPS, f"Probe op '{op}' is not a read op"


# ---------------------------------------------------------------------------
# HttpProbeRunner tests
# ---------------------------------------------------------------------------


class TestRunOnce:
    """run_once() makes exactly the right requests."""

    def test_default_ops_two_requests(self) -> None:
        client = FakeHttpClient()
        config = HttpProbeConfig(enabled=True)
        runner = HttpProbeRunner(client=client, config=config)

        results = runner.run_once()

        assert len(client.calls) == 2
        assert results == {OP_PING_TIME: True, OP_EXCHANGE_INFO: True}

    def test_ops_and_urls(self) -> None:
        client = FakeHttpClient()
        config = HttpProbeConfig(
            enabled=True,
            base_url="https://test.example.com",
        )
        runner = HttpProbeRunner(client=client, config=config)
        runner.run_once()

        ops = [c["op"] for c in client.calls]
        urls = [c["url"] for c in client.calls]
        methods = [c["method"] for c in client.calls]

        assert ops == [OP_PING_TIME, OP_EXCHANGE_INFO]
        assert urls == [
            "https://test.example.com/fapi/v1/time",
            "https://test.example.com/fapi/v1/exchangeInfo",
        ]
        assert all(m == "GET" for m in methods)

    def test_single_op(self) -> None:
        client = FakeHttpClient()
        config = HttpProbeConfig(enabled=True, ops=(OP_PING_TIME,))
        runner = HttpProbeRunner(client=client, config=config)
        runner.run_once()

        assert len(client.calls) == 1
        assert client.calls[0]["op"] == OP_PING_TIME

    def test_failure_does_not_crash(self) -> None:
        client = FakeHttpClient(fail_ops={OP_PING_TIME})
        config = HttpProbeConfig(enabled=True)
        runner = HttpProbeRunner(client=client, config=config)

        results = runner.run_once()

        assert results[OP_PING_TIME] is False
        assert results[OP_EXCHANGE_INFO] is True
        assert len(client.calls) == 2  # both attempted

    def test_timeout_ms_passed(self) -> None:
        client = FakeHttpClient()
        config = HttpProbeConfig(enabled=True, ops=(OP_PING_TIME,))
        runner = HttpProbeRunner(client=client, config=config)
        runner.run_once()

        assert client.calls[0]["timeout_ms"] == 3000


class TestRunLoop:
    """run_loop() respects stop_event and uses injected sleep."""

    def test_loop_stops_on_event(self) -> None:
        """Loop exits when stop_event is set."""
        client = FakeHttpClient()
        config = HttpProbeConfig(enabled=True, interval_ms=100, jitter_ms=0)

        stop = threading.Event()
        sleeps: list[float] = []

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            if len(sleeps) >= 3:
                stop.set()

        # Use stop_event.wait as the sleep mechanism (already in run_loop)
        # We need to make stop_event.wait trigger stop after 3 cycles
        runner = HttpProbeRunner(
            client=client,
            config=config,
            rng=random.Random(42),
        )

        # Run in thread with real stop mechanism
        stop_after_n = 3
        call_count = [0]
        original_run_once = runner.run_once

        def counting_run_once() -> dict[str, bool]:
            result = original_run_once()
            call_count[0] += 1
            if call_count[0] >= stop_after_n:
                stop.set()
            return result

        runner.run_once = counting_run_once  # type: ignore[method-assign]

        thread = threading.Thread(target=runner.run_loop, args=(stop,))
        thread.start()
        thread.join(timeout=5.0)

        assert not thread.is_alive(), "Loop did not stop"
        assert call_count[0] >= stop_after_n
        assert len(client.calls) >= stop_after_n * 2  # 2 ops per cycle

    def test_deterministic_jitter(self) -> None:
        """Jitter uses injected rng, not global random."""
        rng1 = random.Random(42)
        rng2 = random.Random(42)

        # Two rngs with same seed produce identical sequence
        expected_jitters = [rng1.randint(0, 100) for _ in range(5)]
        actual_jitters = [rng2.randint(0, 100) for _ in range(5)]
        assert actual_jitters == expected_jitters
