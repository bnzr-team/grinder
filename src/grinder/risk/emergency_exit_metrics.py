"""Metrics for emergency exit (RISK-EE-1).

Singleton pattern matching fsm_metrics.py / sor_metrics.py.

Gauges (always emitted):
- grinder_emergency_exit_enabled: 1 if feature flag ON, 0 if OFF

Counters (emitted only after first exit):
- grinder_emergency_exit_total{result}: count of exits by result
- grinder_emergency_exit_orders_cancelled_total: cumulative cancelled
- grinder_emergency_exit_positions_closed_total: cumulative closed
"""

from __future__ import annotations

from dataclasses import dataclass, field

from grinder.risk.emergency_exit import EmergencyExitResult  # noqa: TC001 - used at runtime


@dataclass
class EmergencyExitMetrics:
    """Emergency exit metrics singleton."""

    _enabled: bool = False
    _exits: dict[str, int] = field(default_factory=dict)  # result -> count
    _orders_cancelled: int = 0
    _positions_closed: int = 0

    def set_enabled(self, enabled: bool) -> None:
        """Set the enabled gauge value."""
        self._enabled = enabled

    def record_exit(self, result: EmergencyExitResult) -> None:
        """Record an emergency exit result."""
        key = "success" if result.success else "partial"
        self._exits[key] = self._exits.get(key, 0) + 1
        self._orders_cancelled += result.orders_cancelled
        self._positions_closed += result.market_orders_placed

    def to_prometheus_lines(self) -> list[str]:
        """Render Prometheus exposition lines."""
        lines: list[str] = []

        # Gauge: always emitted
        lines.append("# HELP grinder_emergency_exit_enabled Whether emergency exit is enabled")
        lines.append("# TYPE grinder_emergency_exit_enabled gauge")
        lines.append(f"grinder_emergency_exit_enabled {1 if self._enabled else 0}")

        # Counters: only emitted after first exit
        if self._exits:
            lines.append("# HELP grinder_emergency_exit_total Emergency exits by result")
            lines.append("# TYPE grinder_emergency_exit_total counter")
            for result_key in sorted(self._exits):
                lines.append(
                    f'grinder_emergency_exit_total{{result="{result_key}"}} {self._exits[result_key]}'
                )

            lines.append(
                "# HELP grinder_emergency_exit_orders_cancelled_total Cumulative orders cancelled"
            )
            lines.append("# TYPE grinder_emergency_exit_orders_cancelled_total counter")
            lines.append(f"grinder_emergency_exit_orders_cancelled_total {self._orders_cancelled}")

            lines.append(
                "# HELP grinder_emergency_exit_positions_closed_total Cumulative positions closed"
            )
            lines.append("# TYPE grinder_emergency_exit_positions_closed_total counter")
            lines.append(f"grinder_emergency_exit_positions_closed_total {self._positions_closed}")

        return lines


_SINGLETON: EmergencyExitMetrics | None = None


def get_emergency_exit_metrics() -> EmergencyExitMetrics:
    """Get or create the singleton metrics instance."""
    global _SINGLETON  # noqa: PLW0603
    if _SINGLETON is None:
        _SINGLETON = EmergencyExitMetrics()
    return _SINGLETON


def reset_emergency_exit_metrics() -> None:
    """Reset singleton (testing only)."""
    global _SINGLETON  # noqa: PLW0603
    _SINGLETON = None
