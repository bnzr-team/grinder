"""Metrics builder for Prometheus /metrics endpoint.

Consolidates all metrics into a single Prometheus-compatible text output:
- System metrics (uptime, status)
- Gating metrics (allowed/blocked counters)
- Execution metrics (when available)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from grinder.gating import get_gating_metrics


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
