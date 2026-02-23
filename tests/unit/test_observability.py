"""Tests for observability module (metrics builder).

Tests verify:
- System metrics are present (grinder_up, grinder_uptime_seconds)
- Gating metrics are present with correct names and labels
- Contract: metric names and label keys are stable
"""

from __future__ import annotations

import pytest

# Skip entire module if redis not installed (collection won't fail)
pytest.importorskip("redis", reason="redis not installed")

from grinder.gating import GateName, GateReason, get_gating_metrics, reset_gating_metrics
from grinder.gating.metrics import (
    LABEL_GATE,
    LABEL_REASON,
    METRIC_GATING_ALLOWED,
    METRIC_GATING_BLOCKED,
)
from grinder.observability import build_metrics_output
from grinder.observability.metrics_builder import (
    MetricsBuilder,
    get_metrics_builder,
    reset_metrics_builder,
    reset_ready_fn,
    set_ready_fn,
)


class TestMetricsBuilder:
    """Tests for MetricsBuilder class."""

    def test_build_includes_system_metrics(self) -> None:
        """Test that system metrics are included."""
        builder = MetricsBuilder()
        output = builder.build()

        assert "grinder_up 1" in output
        assert "grinder_uptime_seconds" in output

    def test_build_includes_gating_metrics_header(self) -> None:
        """Test that gating metrics HELP/TYPE lines are present."""
        reset_gating_metrics()
        builder = MetricsBuilder()
        output = builder.build()

        assert f"# HELP {METRIC_GATING_ALLOWED}" in output
        assert f"# TYPE {METRIC_GATING_ALLOWED}" in output
        assert f"# HELP {METRIC_GATING_BLOCKED}" in output
        assert f"# TYPE {METRIC_GATING_BLOCKED}" in output

    def test_build_includes_gating_allowed_metrics(self) -> None:
        """Test that allowed gating metrics appear when recorded."""
        reset_gating_metrics()
        metrics = get_gating_metrics()
        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_allowed(GateName.RISK_GATE)

        builder = MetricsBuilder()
        output = builder.build()

        assert f'{METRIC_GATING_ALLOWED}{{{LABEL_GATE}="rate_limiter"}} 1' in output
        assert f'{METRIC_GATING_ALLOWED}{{{LABEL_GATE}="risk_gate"}} 1' in output

    def test_build_includes_gating_blocked_metrics(self) -> None:
        """Test that blocked gating metrics appear when recorded."""
        reset_gating_metrics()
        metrics = get_gating_metrics()
        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.COOLDOWN_ACTIVE)
        metrics.record_blocked(GateName.RISK_GATE, GateReason.MAX_NOTIONAL_EXCEEDED)

        builder = MetricsBuilder()
        output = builder.build()

        assert (
            f'{METRIC_GATING_BLOCKED}{{{LABEL_GATE}="rate_limiter",{LABEL_REASON}="COOLDOWN_ACTIVE"}} 1'
            in output
        )
        assert (
            f'{METRIC_GATING_BLOCKED}{{{LABEL_GATE}="risk_gate",{LABEL_REASON}="MAX_NOTIONAL_EXCEEDED"}} 1'
            in output
        )


class TestMetricsBuilderContract:
    """Contract tests for metrics output format.

    These tests verify that the metrics output conforms to Prometheus format
    and contains the expected metric names and label keys.
    """

    def test_metric_names_present(self) -> None:
        """Verify required metric names are in output."""
        reset_gating_metrics()
        output = build_metrics_output()

        # System metrics
        assert "grinder_up" in output
        assert "grinder_uptime_seconds" in output

        # Gating metric names (in HELP/TYPE comments at minimum)
        assert METRIC_GATING_ALLOWED in output
        assert METRIC_GATING_BLOCKED in output

    def test_label_keys_present_when_data_exists(self) -> None:
        """Verify label keys are correct when metrics are recorded."""
        reset_gating_metrics()
        metrics = get_gating_metrics()
        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_blocked(GateName.RISK_GATE, GateReason.DAILY_LOSS_LIMIT_EXCEEDED)

        output = build_metrics_output()

        # Check label key format
        assert f'{LABEL_GATE}="rate_limiter"' in output
        assert f'{LABEL_GATE}="risk_gate"' in output
        assert f'{LABEL_REASON}="DAILY_LOSS_LIMIT_EXCEEDED"' in output

    def test_prometheus_format_help_type(self) -> None:
        """Verify HELP and TYPE comments follow Prometheus format."""
        reset_gating_metrics()
        output = build_metrics_output()
        lines = output.split("\n")

        # Find HELP lines
        help_lines = [line for line in lines if line.startswith("# HELP")]
        type_lines = [line for line in lines if line.startswith("# TYPE")]

        # Should have HELP/TYPE for system and gating metrics
        assert len(help_lines) >= 4  # grinder_up, uptime, allowed, blocked
        assert len(type_lines) >= 4

        # TYPE should be valid Prometheus types
        valid_types = {"gauge", "counter", "histogram", "summary", "untyped"}
        for line in type_lines:
            parts = line.split()
            assert len(parts) >= 4
            assert parts[3] in valid_types


class TestGlobalMetricsBuilder:
    """Tests for global metrics builder functions."""

    def test_get_metrics_builder_returns_instance(self) -> None:
        """Test get_metrics_builder returns MetricsBuilder."""
        reset_metrics_builder()
        builder = get_metrics_builder()
        assert isinstance(builder, MetricsBuilder)

    def test_global_builder_persists(self) -> None:
        """Test global builder is reused across calls."""
        reset_metrics_builder()
        builder1 = get_metrics_builder()
        builder2 = get_metrics_builder()
        assert builder1 is builder2

    def test_build_metrics_output_convenience(self) -> None:
        """Test build_metrics_output convenience function."""
        reset_gating_metrics()
        reset_metrics_builder()

        output = build_metrics_output()

        assert isinstance(output, str)
        assert "grinder_up" in output


class TestReadyzGauges:
    """Tests for readyz readiness gauges (PR-ALERTS-0)."""

    def setup_method(self) -> None:
        """Reset readyz callback before each test."""
        reset_ready_fn()

    def teardown_method(self) -> None:
        """Reset readyz callback after each test."""
        reset_ready_fn()

    def test_default_no_callback_registered(self) -> None:
        """Without set_ready_fn, both gauges should be 0."""
        builder = MetricsBuilder()
        output = builder.build()

        assert "grinder_readyz_callback_registered 0" in output
        assert "grinder_readyz_ready 0" in output

    def test_callback_returns_true(self) -> None:
        """When callback returns True: registered=1, ready=1."""
        set_ready_fn(lambda: True)

        builder = MetricsBuilder()
        output = builder.build()

        assert "grinder_readyz_callback_registered 1" in output
        assert "grinder_readyz_ready 1" in output

    def test_callback_returns_false(self) -> None:
        """When callback returns False: registered=1, ready=0."""
        set_ready_fn(lambda: False)

        builder = MetricsBuilder()
        output = builder.build()

        assert "grinder_readyz_callback_registered 1" in output
        assert "grinder_readyz_ready 0" in output

    def test_help_type_headers_present(self) -> None:
        """Both gauges should have HELP and TYPE headers."""
        builder = MetricsBuilder()
        output = builder.build()

        assert "# HELP grinder_readyz_callback_registered" in output
        assert "# TYPE grinder_readyz_callback_registered gauge" in output
        assert "# HELP grinder_readyz_ready" in output
        assert "# TYPE grinder_readyz_ready gauge" in output
