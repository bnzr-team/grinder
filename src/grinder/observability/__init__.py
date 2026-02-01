"""Observability module for metrics and health.

Provides:
- MetricsBuilder: Consolidates all metrics into Prometheus format
- build_metrics_output: Convenience function for /metrics endpoint
- build_healthz_body: Pure function for /healthz response
- build_metrics_body: Pure function for /metrics response
"""

from grinder.observability.live_contract import (
    REQUIRED_HEALTHZ_KEYS,
    REQUIRED_METRICS_PATTERNS,
    build_healthz_body,
    build_metrics_body,
    get_start_time,
    reset_start_time,
    set_start_time,
)
from grinder.observability.metrics_builder import MetricsBuilder, build_metrics_output

__all__ = [
    "REQUIRED_HEALTHZ_KEYS",
    "REQUIRED_METRICS_PATTERNS",
    "MetricsBuilder",
    "build_healthz_body",
    "build_metrics_body",
    "build_metrics_output",
    "get_start_time",
    "reset_start_time",
    "set_start_time",
]
