"""Gating module for rate limiting and risk controls.

Provides:
- RateLimiter: Order rate limiting (max per minute, cooldown)
- RiskGate: Position and loss limits (notional, daily loss)
- GatingResult: Standardized result type for gating decisions
"""

from grinder.gating.rate_limiter import RateLimiter
from grinder.gating.risk_gate import RiskGate
from grinder.gating.types import GateReason, GatingResult

__all__ = [
    "GateReason",
    "GatingResult",
    "RateLimiter",
    "RiskGate",
]
