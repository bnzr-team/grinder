"""Observability module for metrics and health.

Provides:
- MetricsBuilder: Consolidates all metrics into Prometheus format
- build_metrics_output: Convenience function for /metrics endpoint
- build_healthz_body: Pure function for /healthz response
- build_readyz_body: Pure function for /readyz response (HA-aware)
- build_metrics_body: Pure function for /metrics response
- RiskMetricsState: State container for risk metrics (kill-switch, drawdown)
"""

from grinder.observability.live_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_HEALTHZ_KEYS,
    REQUIRED_METRICS_PATTERNS,
    REQUIRED_READYZ_KEYS,
    build_healthz_body,
    build_metrics_body,
    build_readyz_body,
    get_start_time,
    reset_start_time,
    set_start_time,
)
from grinder.observability.metrics_builder import (
    MetricsBuilder,
    RiskMetricsState,
    build_metrics_output,
    get_risk_metrics_state,
    reset_risk_metrics_state,
    set_risk_metrics_state,
)

__all__ = [
    "FORBIDDEN_METRIC_LABELS",
    "REQUIRED_HEALTHZ_KEYS",
    "REQUIRED_METRICS_PATTERNS",
    "REQUIRED_READYZ_KEYS",
    "MetricsBuilder",
    "RiskMetricsState",
    "build_healthz_body",
    "build_metrics_body",
    "build_metrics_output",
    "build_readyz_body",
    "get_risk_metrics_state",
    "get_start_time",
    "reset_risk_metrics_state",
    "reset_start_time",
    "set_risk_metrics_state",
    "set_start_time",
]
