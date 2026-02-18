"""Tests for grinder.observability.latency_metrics (Launch-05 PR1).

Covers:
- HttpMetrics: record_request, record_retry, record_fail, record_latency
- Prometheus text output: correct HELP/TYPE/series format
- Label safety: no symbol= in rendered output
- Histogram buckets: correct cumulative counting
- Singleton: get/reset lifecycle
- Contract integration: metrics_builder includes HTTP metrics
"""

from __future__ import annotations

import pytest

from grinder.observability.latency_metrics import (
    METRIC_HTTP_FAIL,
    METRIC_HTTP_LATENCY,
    METRIC_HTTP_REQUESTS,
    METRIC_HTTP_RETRIES,
    HttpMetrics,
    get_http_metrics,
    reset_http_metrics,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    """Reset singleton between tests."""
    reset_http_metrics()


# ---------------------------------------------------------------------------
# Record + render
# ---------------------------------------------------------------------------


class TestHttpMetrics:
    def test_record_request(self) -> None:
        m = HttpMetrics()
        m.record_request("cancel_order", "2xx")
        m.record_request("cancel_order", "2xx")
        m.record_request("cancel_order", "5xx")
        assert m.requests[("cancel_order", "2xx")] == 2
        assert m.requests[("cancel_order", "5xx")] == 1

    def test_record_retry(self) -> None:
        m = HttpMetrics()
        m.record_retry("place_order", "timeout")
        assert m.retries[("place_order", "timeout")] == 1

    def test_record_fail(self) -> None:
        m = HttpMetrics()
        m.record_fail("get_positions", "connect")
        assert m.fails[("get_positions", "connect")] == 1

    def test_record_latency_buckets(self) -> None:
        m = HttpMetrics()
        m.record_latency("cancel_order", 75.0)  # fits in 100 bucket
        buckets = m.latency_buckets["cancel_order"]
        # Should be counted in 100, 200, 400, 800, ...
        assert buckets[50.0] == 0  # 75 > 50
        assert buckets[100.0] == 1
        assert buckets[200.0] == 1
        assert m.latency_count["cancel_order"] == 1
        assert m.latency_sum["cancel_order"] == 75.0

    def test_reset(self) -> None:
        m = HttpMetrics()
        m.record_request("cancel_order", "2xx")
        m.record_latency("cancel_order", 100.0)
        m.reset()
        assert m.requests == {}
        assert m.latency_buckets == {}


# ---------------------------------------------------------------------------
# Prometheus output
# ---------------------------------------------------------------------------


class TestPrometheusOutput:
    def test_empty_renders_help_type(self) -> None:
        m = HttpMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert f"# HELP {METRIC_HTTP_REQUESTS}" in text
        assert f"# TYPE {METRIC_HTTP_REQUESTS}" in text
        assert f"# HELP {METRIC_HTTP_RETRIES}" in text
        assert f"# TYPE {METRIC_HTTP_RETRIES}" in text
        assert f"# HELP {METRIC_HTTP_FAIL}" in text
        assert f"# TYPE {METRIC_HTTP_FAIL}" in text
        assert f"# HELP {METRIC_HTTP_LATENCY}" in text
        assert f"# TYPE {METRIC_HTTP_LATENCY}" in text

    def test_zero_value_placeholders(self) -> None:
        m = HttpMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        # Zero-value placeholders use op="none"
        assert f'{METRIC_HTTP_REQUESTS}{{op="none",status_class="none"}} 0' in text
        assert f'{METRIC_HTTP_RETRIES}{{op="none",reason="none"}} 0' in text
        assert f'{METRIC_HTTP_FAIL}{{op="none",reason="none"}} 0' in text

    def test_request_series(self) -> None:
        m = HttpMetrics()
        m.record_request("cancel_order", "2xx")
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert f'{METRIC_HTTP_REQUESTS}{{op="cancel_order",status_class="2xx"}} 1' in text

    def test_latency_histogram_format(self) -> None:
        m = HttpMetrics()
        m.record_latency("ping_time", 30.0)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        # 30ms fits in 50 bucket and above
        assert f'{METRIC_HTTP_LATENCY}_bucket{{op="ping_time",le="50.0"}} 1' in text
        assert f'{METRIC_HTTP_LATENCY}_bucket{{op="ping_time",le="+Inf"}} 1' in text
        assert f'{METRIC_HTTP_LATENCY}_sum{{op="ping_time"}} 30.0' in text
        assert f'{METRIC_HTTP_LATENCY}_count{{op="ping_time"}} 1' in text


# ---------------------------------------------------------------------------
# Label safety
# ---------------------------------------------------------------------------


class TestLabelSafety:
    def test_no_symbol_in_output(self) -> None:
        m = HttpMetrics()
        m.record_request("cancel_order", "2xx")
        m.record_retry("place_order", "timeout")
        m.record_fail("get_positions", "connect")
        m.record_latency("ping_time", 100.0)
        text = "\n".join(m.to_prometheus_lines())
        assert "symbol=" not in text

    def test_only_expected_labels(self) -> None:
        m = HttpMetrics()
        m.record_request("cancel_order", "2xx")
        m.record_latency("cancel_order", 100.0)
        text = "\n".join(m.to_prometheus_lines())
        # Only op=, status_class=, reason=, le= should appear
        for line in text.split("\n"):
            if "{" in line and not line.startswith("#"):
                assert "op=" in line
                # Check no forbidden labels
                for forbidden in ["symbol=", "order_id=", "key=", "client_id="]:
                    assert forbidden not in line

    def test_real_records_never_use_op_none(self) -> None:
        """op="none" is only for zero-value placeholders, never for real data."""
        m = HttpMetrics()
        m.record_request("cancel_order", "2xx")
        m.record_retry("place_order", "timeout")
        m.record_fail("get_positions", "connect")
        m.record_latency("ping_time", 100.0)
        text = "\n".join(m.to_prometheus_lines())
        # op="none" must not appear once real data is recorded
        assert 'op="none"' not in text


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_returns_same(self) -> None:
        a = get_http_metrics()
        b = get_http_metrics()
        assert a is b

    def test_reset_creates_new(self) -> None:
        a = get_http_metrics()
        reset_http_metrics()
        b = get_http_metrics()
        assert a is not b


# ---------------------------------------------------------------------------
# Contract integration
# ---------------------------------------------------------------------------


class TestContractIntegration:
    def test_metrics_builder_includes_http(self) -> None:
        from grinder.observability.metrics_builder import build_metrics_output  # noqa: PLC0415

        text = build_metrics_output()
        assert f"# HELP {METRIC_HTTP_REQUESTS}" in text
        assert f"# TYPE {METRIC_HTTP_REQUESTS}" in text
        assert f"# HELP {METRIC_HTTP_LATENCY}" in text
        assert f"# TYPE {METRIC_HTTP_LATENCY}" in text
