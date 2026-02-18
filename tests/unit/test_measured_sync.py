"""Tests for grinder.net.measured_sync (Launch-05 PR2).

Covers:
- Disabled mode: pure pass-through (byte-for-byte same behavior)
- Per-op deadline: timeout_ms overridden from DeadlinePolicy
- Retry matrix: transient errors retried, non-retryable errors immediate
- Label safety: no symbol= in metrics output
- Metrics recording: requests, retries, fails, latency
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from grinder.execution.binance_port import HttpResponse
from grinder.net.measured_sync import MeasuredSyncHttpClient
from grinder.net.retry_policy import (
    OP_CANCEL_ORDER,
    OP_GET_POSITIONS,
    DeadlinePolicy,
    HttpRetryPolicy,
)
from grinder.observability.latency_metrics import HttpMetrics

# ---------------------------------------------------------------------------
# Deterministic test clock + sleep
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic clock for testing."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeSleep:
    """Deterministic sleep that tracks calls and advances clock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


# ---------------------------------------------------------------------------
# Mock inner HttpClient
# ---------------------------------------------------------------------------


@dataclass
class MockInnerClient:
    """Mock HttpClient that returns configured responses in sequence."""

    responses: list[HttpResponse] = field(default_factory=list)
    errors: list[Exception | None] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _call_index: int = field(default=0, repr=False)

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "timeout_ms": timeout_ms,
                "op": op,
            }
        )

        idx = self._call_index
        self._call_index += 1

        # Raise error if configured
        if idx < len(self.errors) and self.errors[idx] is not None:
            raise self.errors[idx]  # type: ignore[misc]

        # Return configured response
        if idx < len(self.responses):
            return self.responses[idx]

        # Default success
        return HttpResponse(status_code=200, json_data={})


# ---------------------------------------------------------------------------
# Disabled mode (pass-through)
# ---------------------------------------------------------------------------


class TestDisabledPassthrough:
    """When enabled=False, MeasuredSyncHttpClient is pure pass-through."""

    def test_disabled_is_zero_behavior_change(self) -> None:
        """THE proof: enabled=False → 1 call, 0 sleeps, 0 metrics, timeout unchanged."""
        metrics = HttpMetrics()
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={"ok": True})])
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=False,
            metrics=metrics,
            clock=clock,
            sleep_func=sleep,
        )

        result = client.request(
            method="GET",
            url="https://api.binance.com/fapi/v2/positionRisk",
            params={"symbol": "BTCUSDT"},
            headers={"X-MBX-APIKEY": "key"},
            timeout_ms=3000,
            op=OP_GET_POSITIONS,
        )

        assert result.status_code == 200
        assert result.json_data == {"ok": True}

        # 1) Underlying client called exactly 1 time
        assert len(inner.calls) == 1
        call = inner.calls[0]
        assert call["method"] == "GET"
        assert call["timeout_ms"] == 3000  # NOT overridden by DeadlinePolicy
        assert call["op"] == OP_GET_POSITIONS

        # 2) sleep_func never called (0 retries)
        assert sleep.calls == []

        # 3) No metrics recorded
        assert metrics.requests == {}
        assert metrics.retries == {}
        assert metrics.fails == {}
        assert metrics.latency_buckets == {}
        assert metrics.latency_sum == {}
        assert metrics.latency_count == {}

    def test_empty_op_is_passthrough_even_when_enabled(self) -> None:
        """When op="" (not annotated), pass through even if enabled=True."""
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={})])
        client = MeasuredSyncHttpClient(inner=inner, enabled=True)

        client.request(method="GET", url="/test", timeout_ms=3000, op="")

        call = inner.calls[0]
        assert call["timeout_ms"] == 3000  # NOT overridden
        assert len(inner.calls) == 1


# ---------------------------------------------------------------------------
# Per-op deadline
# ---------------------------------------------------------------------------


class TestPerOpDeadline:
    """When enabled=True, timeout_ms is overridden by DeadlinePolicy."""

    def test_cancel_order_uses_deadline(self) -> None:
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={})])
        clock = FakeClock()
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            deadline_policy=DeadlinePolicy.defaults(),
            clock=clock,
        )

        client.request(
            method="DELETE",
            url="/fapi/v1/order",
            timeout_ms=5000,  # Would be used if disabled
            op=OP_CANCEL_ORDER,
        )

        # Inner client should receive deadline from policy (600ms), not the original 5000ms
        call = inner.calls[0]
        assert call["timeout_ms"] == 600

    def test_get_positions_uses_deadline(self) -> None:
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={})])
        clock = FakeClock()
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            deadline_policy=DeadlinePolicy.defaults(),
            clock=clock,
        )

        client.request(method="GET", url="/fapi/v2/positionRisk", op=OP_GET_POSITIONS)

        call = inner.calls[0]
        assert call["timeout_ms"] == 2500  # DeadlinePolicy default for get_positions

    def test_custom_deadline_override(self) -> None:
        """HTTP_DEADLINE_CANCEL_ORDER_MS=400 should override default 600ms."""
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={})])
        clock = FakeClock()
        custom_policy = DeadlinePolicy(deadlines={"cancel_order": 400})
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            deadline_policy=custom_policy,
            clock=clock,
        )

        client.request(method="DELETE", url="/fapi/v1/order", op=OP_CANCEL_ORDER)

        call = inner.calls[0]
        assert call["timeout_ms"] == 400


# ---------------------------------------------------------------------------
# Retry matrix
# ---------------------------------------------------------------------------


class TestRetryMatrix:
    """Retries on transient errors, immediate failure on non-retryable."""

    def test_retry_on_5xx(self) -> None:
        """5xx is retryable → retries, then succeeds."""
        inner = MockInnerClient(
            responses=[
                HttpResponse(status_code=503, json_data={}),
                HttpResponse(status_code=200, json_data={"ok": True}),
            ]
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            clock=clock,
            sleep_func=sleep,
        )

        result = client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert result.status_code == 200
        assert result.json_data == {"ok": True}
        assert len(inner.calls) == 2  # 1 retry
        assert len(sleep.calls) == 1  # 1 delay

    def test_no_retry_on_4xx(self) -> None:
        """4xx is not retryable → immediate return."""
        inner = MockInnerClient(
            responses=[HttpResponse(status_code=400, json_data={"code": -1111})]
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            clock=clock,
            sleep_func=sleep,
        )

        result = client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert result.status_code == 400
        assert len(inner.calls) == 1  # No retries
        assert len(sleep.calls) == 0

    def test_retry_on_timeout_exception(self) -> None:
        """TimeoutError is retryable → retries, then succeeds."""
        inner = MockInnerClient(
            responses=[HttpResponse(status_code=200, json_data={"ok": True})],
            errors=[TimeoutError("timed out"), None],
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            clock=clock,
            sleep_func=sleep,
        )

        result = client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert result.status_code == 200
        assert len(inner.calls) == 2
        assert len(sleep.calls) == 1

    def test_exhausted_retries_raises(self) -> None:
        """All retries exhausted on exception → raises."""
        inner = MockInnerClient(errors=[TimeoutError("t1"), TimeoutError("t2"), TimeoutError("t3")])
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            clock=clock,
            sleep_func=sleep,
        )

        with pytest.raises(TimeoutError):
            client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert len(inner.calls) == 3
        assert len(sleep.calls) == 2  # 2 delays between 3 attempts

    def test_backoff_is_deterministic(self) -> None:
        """Verify exponential backoff delays with jitter=False."""
        inner = MockInnerClient(
            responses=[
                HttpResponse(status_code=503, json_data={}),
                HttpResponse(status_code=503, json_data={}),
                HttpResponse(status_code=200, json_data={}),
            ]
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy(
                max_attempts=3,
                base_delay_ms=100,
                max_delay_ms=500,
                backoff_multiplier=2.0,
                jitter=False,
            ),
            clock=clock,
            sleep_func=sleep,
        )

        client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        # Delays: attempt 0 → 100ms, attempt 1 → 200ms
        assert sleep.calls == [0.1, 0.2]

    def test_no_retry_when_max_attempts_is_1(self) -> None:
        """max_attempts=1 means no retries even for retryable errors."""
        inner = MockInnerClient(responses=[HttpResponse(status_code=503, json_data={})])
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=1),
            clock=clock,
            sleep_func=sleep,
        )

        result = client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert result.status_code == 503
        assert len(inner.calls) == 1
        assert len(sleep.calls) == 0


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    """Verify metrics are recorded correctly."""

    def test_success_records_request_and_latency(self) -> None:
        metrics = HttpMetrics()
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={})])
        clock = FakeClock()
        client = MeasuredSyncHttpClient(inner=inner, enabled=True, metrics=metrics, clock=clock)

        client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert metrics.requests[(OP_GET_POSITIONS, "2xx")] == 1
        assert metrics.latency_count[OP_GET_POSITIONS] == 1

    def test_retry_records_retry_metric(self) -> None:
        metrics = HttpMetrics()
        inner = MockInnerClient(
            responses=[
                HttpResponse(status_code=503, json_data={}),
                HttpResponse(status_code=200, json_data={}),
            ]
        )
        clock = FakeClock()
        sleep = FakeSleep(clock)
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            metrics=metrics,
            clock=clock,
            sleep_func=sleep,
        )

        client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        # First attempt: 503 → retry recorded
        assert metrics.retries[(OP_GET_POSITIONS, "5xx")] == 1
        # Second attempt: 200 → request recorded
        assert metrics.requests[(OP_GET_POSITIONS, "2xx")] == 1

    def test_failure_records_fail_metric(self) -> None:
        metrics = HttpMetrics()
        inner = MockInnerClient(responses=[HttpResponse(status_code=400, json_data={})])
        clock = FakeClock()
        client = MeasuredSyncHttpClient(
            inner=inner,
            enabled=True,
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            metrics=metrics,
            clock=clock,
        )

        client.request(method="GET", url="/test", op=OP_GET_POSITIONS)

        assert metrics.fails[(OP_GET_POSITIONS, "4xx")] == 1


# ---------------------------------------------------------------------------
# Label safety
# ---------------------------------------------------------------------------


class TestLabelSafety:
    """No symbol= or other forbidden labels in metrics output."""

    def test_no_symbol_in_prometheus_output(self) -> None:
        metrics = HttpMetrics()
        inner = MockInnerClient(responses=[HttpResponse(status_code=200, json_data={})])
        clock = FakeClock()
        client = MeasuredSyncHttpClient(inner=inner, enabled=True, metrics=metrics, clock=clock)

        client.request(method="GET", url="/test", op=OP_GET_POSITIONS)
        client.request(method="DELETE", url="/test", op=OP_CANCEL_ORDER)

        text = "\n".join(metrics.to_prometheus_lines())
        assert "symbol=" not in text
        for line in text.split("\n"):
            if "{" in line and not line.startswith("#"):
                assert "op=" in line
                for forbidden in ["symbol=", "order_id=", "key=", "client_id="]:
                    assert forbidden not in line


# ---------------------------------------------------------------------------
# Static coverage: all BinanceFuturesPort call sites have op=OP_
# ---------------------------------------------------------------------------


class TestStaticOpCoverage:
    """Verify every http_client.request() in BinanceFuturesPort passes op=OP_."""

    def test_all_call_sites_have_op(self) -> None:
        """Every http_client.request() in BinanceFuturesPort must have op=OP_."""
        port_file = pathlib.Path(__file__).resolve().parents[2] / (
            "src/grinder/execution/binance_futures_port.py"
        )
        lines = port_file.read_text().splitlines()

        # Find line numbers of each call site
        call_lines: list[int] = []
        for i, line in enumerate(lines):
            if "self.http_client.request(" in line:
                call_lines.append(i)

        assert len(call_lines) >= 12, f"Expected >=12 call sites, found {len(call_lines)}"

        # For each call site, look within the next 10 lines for op=OP_
        for line_no in call_lines:
            window = "\n".join(lines[line_no : line_no + 10])
            assert "op=OP_" in window, (
                f"Call site at line {line_no + 1} missing op=OP_* annotation:\n{window}"
            )
