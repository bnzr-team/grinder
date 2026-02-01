"""Contract tests for gating module.

These tests verify that gating contracts (reason codes, metric labels) remain stable.
If these tests fail, it means a breaking change was made to the gating contract.

To update the contract after intentional changes:
1. Update the expected values in this file
2. Add an entry to docs/DECISIONS.md explaining the change
"""

from __future__ import annotations

from grinder.gating import (
    ALL_GATE_NAMES,
    ALL_GATE_REASONS,
    GateName,
    GateReason,
    GatingMetrics,
    get_gating_metrics,
    reset_gating_metrics,
)
from grinder.gating.metrics import (
    LABEL_GATE,
    LABEL_REASON,
    METRIC_GATING_ALLOWED,
    METRIC_GATING_BLOCKED,
)


class TestGateReasonContract:
    """Contract tests for GateReason enum.

    These tests ensure that reason codes remain stable across versions.
    Changing these values is a breaking change that affects:
    - Metric labels (gating_blocked_total{reason=...})
    - JSON serialization (GatingResult.to_dict())
    - Log messages and observability
    """

    # Canonical expected reason codes
    EXPECTED_REASONS = frozenset(
        {
            "PASS",
            "RATE_LIMIT_EXCEEDED",
            "COOLDOWN_ACTIVE",
            "MAX_NOTIONAL_EXCEEDED",
            "DAILY_LOSS_LIMIT_EXCEEDED",
            "MAX_ORDERS_EXCEEDED",
            # Toxicity gate reasons (v0)
            "SPREAD_SPIKE",
            "PRICE_IMPACT_HIGH",
            # Kill-switch reasons (v0 - ADR-013)
            "KILL_SWITCH_ACTIVE",
            "DRAWDOWN_LIMIT_EXCEEDED",
        }
    )

    def test_all_reasons_are_expected(self) -> None:
        """Verify no unexpected reason codes were added."""
        actual = ALL_GATE_REASONS
        unexpected = actual - self.EXPECTED_REASONS
        assert not unexpected, f"Unexpected reason codes added: {unexpected}"

    def test_no_reasons_removed(self) -> None:
        """Verify no expected reason codes were removed."""
        actual = ALL_GATE_REASONS
        missing = self.EXPECTED_REASONS - actual
        assert not missing, f"Expected reason codes removed: {missing}"

    def test_reason_values_match_names(self) -> None:
        """Verify reason enum values match their names (convention)."""
        for reason in GateReason:
            assert reason.value == reason.name, (
                f"Reason {reason.name} has value {reason.value!r} "
                f"but should match name {reason.name!r}"
            )


class TestGateNameContract:
    """Contract tests for GateName enum.

    These tests ensure that gate names remain stable across versions.
    Changing these values is a breaking change that affects:
    - Metric labels (gating_allowed_total{gate=...})
    """

    # Canonical expected gate names
    EXPECTED_NAMES = frozenset(
        {
            "rate_limiter",
            "risk_gate",
            "toxicity_gate",
        }
    )

    def test_all_names_are_expected(self) -> None:
        """Verify no unexpected gate names were added."""
        actual = ALL_GATE_NAMES
        unexpected = actual - self.EXPECTED_NAMES
        assert not unexpected, f"Unexpected gate names added: {unexpected}"

    def test_no_names_removed(self) -> None:
        """Verify no expected gate names were removed."""
        actual = ALL_GATE_NAMES
        missing = self.EXPECTED_NAMES - actual
        assert not missing, f"Expected gate names removed: {missing}"


class TestMetricNamesContract:
    """Contract tests for metric names and labels.

    These tests ensure metric contracts remain stable for Prometheus/Grafana.
    Changing these values is a breaking change that affects:
    - Grafana dashboards
    - Alert rules
    - Any systems scraping /metrics
    """

    def test_metric_name_gating_allowed(self) -> None:
        """Verify allowed metric name is stable."""
        assert METRIC_GATING_ALLOWED == "grinder_gating_allowed_total"

    def test_metric_name_gating_blocked(self) -> None:
        """Verify blocked metric name is stable."""
        assert METRIC_GATING_BLOCKED == "grinder_gating_blocked_total"

    def test_label_key_gate(self) -> None:
        """Verify gate label key is stable."""
        assert LABEL_GATE == "gate"

    def test_label_key_reason(self) -> None:
        """Verify reason label key is stable."""
        assert LABEL_REASON == "reason"


class TestGatingMetrics:
    """Tests for GatingMetrics class."""

    def test_record_allowed(self) -> None:
        """Test recording allowed decisions."""
        metrics = GatingMetrics()

        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_allowed(GateName.RISK_GATE)

        assert metrics.get_allowed_count(GateName.RATE_LIMITER) == 2
        assert metrics.get_allowed_count(GateName.RISK_GATE) == 1

    def test_record_blocked(self) -> None:
        """Test recording blocked decisions."""
        metrics = GatingMetrics()

        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.COOLDOWN_ACTIVE)
        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.RATE_LIMIT_EXCEEDED)
        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.COOLDOWN_ACTIVE)
        metrics.record_blocked(GateName.RISK_GATE, GateReason.MAX_NOTIONAL_EXCEEDED)

        assert metrics.get_blocked_count(GateName.RATE_LIMITER, GateReason.COOLDOWN_ACTIVE) == 2
        assert metrics.get_blocked_count(GateName.RATE_LIMITER, GateReason.RATE_LIMIT_EXCEEDED) == 1
        assert metrics.get_blocked_count(GateName.RISK_GATE, GateReason.MAX_NOTIONAL_EXCEEDED) == 1

    def test_get_blocked_count_all_reasons(self) -> None:
        """Test getting total blocked count for a gate (all reasons)."""
        metrics = GatingMetrics()

        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.COOLDOWN_ACTIVE)
        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.RATE_LIMIT_EXCEEDED)
        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.RATE_LIMIT_EXCEEDED)

        assert metrics.get_blocked_count(GateName.RATE_LIMITER) == 3

    def test_get_metrics_structure(self) -> None:
        """Test get_metrics returns correct structure."""
        metrics = GatingMetrics()

        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_blocked(GateName.RISK_GATE, GateReason.MAX_NOTIONAL_EXCEEDED)

        result = metrics.get_metrics()

        assert METRIC_GATING_ALLOWED in result
        assert METRIC_GATING_BLOCKED in result

    def test_to_prometheus_lines_format(self) -> None:
        """Test Prometheus text format output."""
        metrics = GatingMetrics()

        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_blocked(GateName.RATE_LIMITER, GateReason.COOLDOWN_ACTIVE)

        lines = metrics.to_prometheus_lines()

        # Check HELP/TYPE comments are present
        assert any("# HELP" in line and METRIC_GATING_ALLOWED in line for line in lines)
        assert any("# TYPE" in line and METRIC_GATING_ALLOWED in line for line in lines)

        # Check metric lines have correct format
        allowed_line = next(
            (line for line in lines if line.startswith(METRIC_GATING_ALLOWED + "{")),
            None,
        )
        assert allowed_line is not None
        assert 'gate="rate_limiter"' in allowed_line
        assert allowed_line.endswith(" 1")

        blocked_line = next(
            (line for line in lines if line.startswith(METRIC_GATING_BLOCKED + "{")),
            None,
        )
        assert blocked_line is not None
        assert 'gate="rate_limiter"' in blocked_line
        assert 'reason="COOLDOWN_ACTIVE"' in blocked_line
        assert blocked_line.endswith(" 1")

    def test_reset(self) -> None:
        """Test reset clears all metrics."""
        metrics = GatingMetrics()

        metrics.record_allowed(GateName.RATE_LIMITER)
        metrics.record_blocked(GateName.RISK_GATE, GateReason.MAX_NOTIONAL_EXCEEDED)

        metrics.reset()

        assert metrics.get_allowed_count(GateName.RATE_LIMITER) == 0
        assert metrics.get_blocked_count(GateName.RISK_GATE) == 0


class TestGlobalMetricsInstance:
    """Tests for global metrics functions."""

    def test_get_gating_metrics_returns_instance(self) -> None:
        """Test get_gating_metrics returns a GatingMetrics instance."""
        reset_gating_metrics()
        metrics = get_gating_metrics()

        assert isinstance(metrics, GatingMetrics)

    def test_global_metrics_persist(self) -> None:
        """Test global metrics persist across calls."""
        reset_gating_metrics()

        metrics1 = get_gating_metrics()
        metrics1.record_allowed(GateName.RATE_LIMITER)

        metrics2 = get_gating_metrics()
        assert metrics2.get_allowed_count(GateName.RATE_LIMITER) == 1
