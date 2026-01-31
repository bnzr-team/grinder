"""Gating module for rate limiting and risk controls.

Provides:
- RateLimiter: Order rate limiting (max per minute, cooldown)
- RiskGate: Position and loss limits (notional, daily loss)
- GatingResult: Standardized result type for gating decisions
- GateName: Identifier enum for gates (stable metric labels)
- GatingMetrics: Metrics collector for gating decisions
"""

from grinder.gating.metrics import (
    GatingMetrics,
    get_gating_metrics,
    reset_gating_metrics,
)
from grinder.gating.rate_limiter import RateLimiter
from grinder.gating.risk_gate import RiskGate
from grinder.gating.types import (
    ALL_GATE_NAMES,
    ALL_GATE_REASONS,
    GateName,
    GateReason,
    GatingResult,
)

__all__ = [
    "ALL_GATE_NAMES",
    "ALL_GATE_REASONS",
    "GateName",
    "GateReason",
    "GatingMetrics",
    "GatingResult",
    "RateLimiter",
    "RiskGate",
    "get_gating_metrics",
    "reset_gating_metrics",
]
