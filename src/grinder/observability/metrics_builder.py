"""Metrics builder for Prometheus /metrics endpoint.

Consolidates all metrics into a single Prometheus-compatible text output:
- System metrics (uptime, status)
- Gating metrics (allowed/blocked counters)
- Risk metrics (kill-switch, drawdown)
- HA metrics (role)
- Connector metrics (retries, idempotency, circuit breaker) - H5
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from grinder.connectors.metrics import get_connector_metrics
from grinder.gating import get_gating_metrics
from grinder.ha.role import HARole, get_ha_state
from grinder.reconcile.metrics import get_reconcile_metrics


@dataclass
class RiskMetricsState:
    """Current state of risk metrics for Prometheus export.

    Attributes:
        kill_switch_triggered: Whether kill-switch is active (0 or 1)
        kill_switch_trips: Total trips by reason {reason: count}
        drawdown_pct: Current drawdown percentage (0-100)
        high_water_mark: Current equity HWM
    """

    kill_switch_triggered: int = 0
    kill_switch_trips: dict[str, int] = field(default_factory=dict)
    drawdown_pct: float = 0.0
    high_water_mark: Decimal = Decimal("0")


# Module-level state for risk metrics (updated by external caller)
_risk_state: list[RiskMetricsState | None] = [None]


def set_risk_metrics_state(state: RiskMetricsState) -> None:
    """Set current risk metrics state (called by engine/server)."""
    _risk_state[0] = state


def get_risk_metrics_state() -> RiskMetricsState | None:
    """Get current risk metrics state."""
    return _risk_state[0]


def reset_risk_metrics_state() -> None:
    """Reset risk metrics state (for testing)."""
    _risk_state[0] = None


@dataclass
class MetricsBuilder:
    """Builds consolidated Prometheus metrics output.

    Attributes:
        start_time: Server start time for uptime calculation.
    """

    start_time: float = field(default_factory=time.time)

    def build(self) -> str:
        """Build complete metrics output in Prometheus text format.

        Returns:
            Prometheus-compatible metrics text.
        """
        lines: list[str] = []

        # System metrics
        lines.extend(self._build_system_metrics())

        # Gating metrics
        lines.extend(self._build_gating_metrics())

        # Risk metrics (if available)
        lines.extend(self._build_risk_metrics())

        # HA metrics
        lines.extend(self._build_ha_metrics())

        # Connector metrics (H2/H3/H4)
        lines.extend(self._build_connector_metrics())

        # Reconcile metrics (LC-09b/LC-10/LC-11/LC-15b)
        lines.extend(self._build_reconcile_metrics())

        return "\n".join(lines)

    def _build_system_metrics(self) -> list[str]:
        """Build system-level metrics."""
        uptime = time.time() - self.start_time
        return [
            "# HELP grinder_up Whether grinder is running",
            "# TYPE grinder_up gauge",
            "grinder_up 1",
            "# HELP grinder_uptime_seconds Uptime in seconds",
            "# TYPE grinder_uptime_seconds gauge",
            f"grinder_uptime_seconds {uptime:.2f}",
        ]

    def _build_gating_metrics(self) -> list[str]:
        """Build gating metrics from global gating metrics instance."""
        gating_metrics = get_gating_metrics()
        return gating_metrics.to_prometheus_lines()

    def _build_risk_metrics(self) -> list[str]:
        """Build risk metrics (kill-switch, drawdown)."""
        state = get_risk_metrics_state()
        if state is None:
            # Return metrics with default values (risk module not active)
            return [
                "# HELP grinder_kill_switch_triggered Whether kill-switch is active",
                "# TYPE grinder_kill_switch_triggered gauge",
                "grinder_kill_switch_triggered 0",
                "# HELP grinder_kill_switch_trips_total Total kill-switch trips by reason",
                "# TYPE grinder_kill_switch_trips_total counter",
                "# HELP grinder_drawdown_pct Current drawdown percentage",
                "# TYPE grinder_drawdown_pct gauge",
                "grinder_drawdown_pct 0",
                "# HELP grinder_high_water_mark Current equity high-water mark",
                "# TYPE grinder_high_water_mark gauge",
                "grinder_high_water_mark 0",
            ]

        lines = [
            "# HELP grinder_kill_switch_triggered Whether kill-switch is active",
            "# TYPE grinder_kill_switch_triggered gauge",
            f"grinder_kill_switch_triggered {state.kill_switch_triggered}",
            "# HELP grinder_kill_switch_trips_total Total kill-switch trips by reason",
            "# TYPE grinder_kill_switch_trips_total counter",
        ]

        # Add trip counts by reason
        for reason, count in sorted(state.kill_switch_trips.items()):
            lines.append(f'grinder_kill_switch_trips_total{{reason="{reason}"}} {count}')

        # If no trips yet, add a zero-value entry for visibility
        if not state.kill_switch_trips:
            lines.append('grinder_kill_switch_trips_total{reason="none"} 0')

        lines.extend(
            [
                "# HELP grinder_drawdown_pct Current drawdown percentage",
                "# TYPE grinder_drawdown_pct gauge",
                f"grinder_drawdown_pct {state.drawdown_pct:.2f}",
                "# HELP grinder_high_water_mark Current equity high-water mark",
                "# TYPE grinder_high_water_mark gauge",
                f"grinder_high_water_mark {float(state.high_water_mark):.2f}",
            ]
        )

        return lines

    def _build_ha_metrics(self) -> list[str]:
        """Build HA (high availability) metrics.

        Outputs all possible roles with 1 for current role, 0 for others.
        This follows Prometheus best practices for enum-like gauges.
        """
        current_role = get_ha_state().role
        lines = [
            "# HELP grinder_ha_role Current HA role (1 = this role, 0 = other roles)",
            "# TYPE grinder_ha_role gauge",
        ]
        for role in HARole:
            value = 1 if role == current_role else 0
            lines.append(f'grinder_ha_role{{role="{role.value}"}} {value}')
        return lines

    def _build_connector_metrics(self) -> list[str]:
        """Build connector metrics (H2 retries, H3 idempotency, H4 circuit breaker)."""
        connector_metrics = get_connector_metrics()
        return connector_metrics.to_prometheus_lines()

    def _build_reconcile_metrics(self) -> list[str]:
        """Build reconcile metrics (LC-09b/LC-10/LC-11/LC-15b)."""
        reconcile_metrics = get_reconcile_metrics()
        return reconcile_metrics.to_prometheus_lines()


class _BuilderHolder:
    """Holder for global metrics builder instance."""

    instance: MetricsBuilder | None = None


def get_metrics_builder() -> MetricsBuilder:
    """Get or create global metrics builder instance."""
    if _BuilderHolder.instance is None:
        _BuilderHolder.instance = MetricsBuilder()
    return _BuilderHolder.instance


def reset_metrics_builder() -> None:
    """Reset global metrics builder (for testing)."""
    _BuilderHolder.instance = None


def build_metrics_output() -> str:
    """Convenience function to build metrics output.

    Returns:
        Prometheus-compatible metrics text.
    """
    return get_metrics_builder().build()
