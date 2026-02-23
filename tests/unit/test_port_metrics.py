"""Tests for execution port metrics (PR-FUT-1).

Tests verify:
- PortMetrics records order attempts correctly
- Prometheus output includes all required patterns
- Zero-series initialization works
- Global singleton lifecycle
- MetricsBuilder integration
"""

from __future__ import annotations

import pytest

from grinder.execution.port_metrics import (
    METRIC_PORT_ORDER_ATTEMPTS,
    PortMetrics,
    get_port_metrics,
    reset_port_metrics,
)


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    """Reset port metrics before each test."""
    reset_port_metrics()


class TestPortMetrics:
    """Tests for PortMetrics dataclass."""

    def test_record_order_attempt(self) -> None:
        """Test recording order attempts."""
        metrics = PortMetrics()
        metrics.record_order_attempt("futures", "place")
        metrics.record_order_attempt("futures", "place")
        metrics.record_order_attempt("futures", "cancel")

        assert metrics.order_attempts[("futures", "place")] == 2
        assert metrics.order_attempts[("futures", "cancel")] == 1

    def test_record_multiple_ports(self) -> None:
        """Test recording attempts across different ports."""
        metrics = PortMetrics()
        metrics.record_order_attempt("futures", "place")
        metrics.record_order_attempt("noop", "place")

        assert metrics.order_attempts[("futures", "place")] == 1
        assert metrics.order_attempts[("noop", "place")] == 1

    def test_initialize_zero_series(self) -> None:
        """Test zero-series initialization for a port."""
        metrics = PortMetrics()
        metrics.initialize_zero_series("futures")

        assert metrics.order_attempts[("futures", "place")] == 0
        assert metrics.order_attempts[("futures", "cancel")] == 0
        assert metrics.order_attempts[("futures", "replace")] == 0

    def test_initialize_zero_series_idempotent(self) -> None:
        """Test that zero-series init doesn't reset existing counters."""
        metrics = PortMetrics()
        metrics.record_order_attempt("futures", "place")
        metrics.record_order_attempt("futures", "place")

        # Initialize should NOT reset the already-incremented counter
        metrics.initialize_zero_series("futures")

        assert metrics.order_attempts[("futures", "place")] == 2
        assert metrics.order_attempts[("futures", "cancel")] == 0
        assert metrics.order_attempts[("futures", "replace")] == 0

    def test_reset(self) -> None:
        """Test reset clears all data."""
        metrics = PortMetrics()
        metrics.record_order_attempt("futures", "place")
        metrics.reset()

        assert len(metrics.order_attempts) == 0


class TestPrometheusOutput:
    """Tests for Prometheus text format output."""

    def test_to_prometheus_lines_empty(self) -> None:
        """Test Prometheus output with no recorded events."""
        metrics = PortMetrics()
        lines = metrics.to_prometheus_lines()
        output = "\n".join(lines)

        assert f"# HELP {METRIC_PORT_ORDER_ATTEMPTS}" in output
        assert f"# TYPE {METRIC_PORT_ORDER_ATTEMPTS}" in output
        # Fallback placeholder when no data
        assert '{port="none",op="none"} 0' in output

    def test_to_prometheus_lines_with_data(self) -> None:
        """Test Prometheus output with recorded events."""
        metrics = PortMetrics()
        metrics.record_order_attempt("futures", "place")
        metrics.record_order_attempt("futures", "cancel")

        lines = metrics.to_prometheus_lines()
        output = "\n".join(lines)

        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="cancel"}} 1' in output
        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="place"}} 1' in output

    def test_to_prometheus_lines_zero_series(self) -> None:
        """Test Prometheus output with zero-initialized series."""
        metrics = PortMetrics()
        metrics.initialize_zero_series("futures")

        lines = metrics.to_prometheus_lines()
        output = "\n".join(lines)

        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="place"}} 0' in output
        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="cancel"}} 0' in output
        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="replace"}} 0' in output
        # Should NOT have the fallback placeholder
        assert '{port="none"' not in output


class TestGlobalSingleton:
    """Tests for global port metrics singleton."""

    def test_get_port_metrics_returns_instance(self) -> None:
        """Test that get_port_metrics returns a PortMetrics instance."""
        metrics = get_port_metrics()
        assert isinstance(metrics, PortMetrics)

    def test_global_metrics_persists(self) -> None:
        """Test that global metrics is reused across calls."""
        metrics1 = get_port_metrics()
        metrics2 = get_port_metrics()
        assert metrics1 is metrics2

    def test_reset_port_metrics(self) -> None:
        """Test that reset clears the singleton."""
        metrics1 = get_port_metrics()
        metrics1.record_order_attempt("futures", "place")

        reset_port_metrics()
        metrics2 = get_port_metrics()

        assert metrics1 is not metrics2
        assert ("futures", "place") not in metrics2.order_attempts


class TestMetricsBuilderIntegration:
    """Tests for MetricsBuilder integration with port metrics."""

    def test_build_includes_port_metrics(self) -> None:
        """Test that MetricsBuilder includes port metrics."""
        pytest.importorskip("redis", reason="redis not installed")

        from grinder.gating import reset_gating_metrics  # noqa: PLC0415
        from grinder.observability import build_metrics_output  # noqa: PLC0415
        from grinder.observability.metrics_builder import reset_metrics_builder  # noqa: PLC0415

        reset_gating_metrics()
        reset_metrics_builder()
        reset_port_metrics()

        # Initialize zero series (as run_trading.py does)
        metrics = get_port_metrics()
        metrics.initialize_zero_series("futures")

        output = build_metrics_output()

        assert f"# HELP {METRIC_PORT_ORDER_ATTEMPTS}" in output
        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="place"}} 0' in output
        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="cancel"}} 0' in output
        assert f'{METRIC_PORT_ORDER_ATTEMPTS}{{port="futures",op="replace"}} 0' in output
