"""Observability module for metrics and health.

Provides:
- MetricsBuilder: Consolidates all metrics into Prometheus format
- build_metrics_output: Convenience function for /metrics endpoint
"""

from grinder.observability.metrics_builder import MetricsBuilder, build_metrics_output

__all__ = [
    "MetricsBuilder",
    "build_metrics_output",
]
