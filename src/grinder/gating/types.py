"""Gating types and contracts.

Defines the result types for gating decisions (rate limit, risk checks).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class GateName(Enum):
    """Identifier for which gate made the decision.

    These values are stable and used as metric labels.
    DO NOT rename or remove values without updating metric contracts.
    """

    RATE_LIMITER = "rate_limiter"
    RISK_GATE = "risk_gate"
    TOXICITY_GATE = "toxicity_gate"


class GateReason(Enum):
    """Reason for gating decision.

    These values are stable and used as metric labels.
    DO NOT rename or remove values without updating metric contracts.
    """

    PASS = "PASS"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    MAX_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXCEEDED"
    DAILY_LOSS_LIMIT_EXCEEDED = "DAILY_LOSS_LIMIT_EXCEEDED"
    MAX_ORDERS_EXCEEDED = "MAX_ORDERS_EXCEEDED"
    # Toxicity gate reasons (v0)
    SPREAD_SPIKE = "SPREAD_SPIKE"
    PRICE_IMPACT_HIGH = "PRICE_IMPACT_HIGH"
    # Kill-switch reasons (v0 - ADR-013)
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DRAWDOWN_LIMIT_EXCEEDED = "DRAWDOWN_LIMIT_EXCEEDED"


# Canonical list of all gate names for contract testing
ALL_GATE_NAMES: frozenset[str] = frozenset(g.value for g in GateName)

# Canonical list of all gate reasons for contract testing
ALL_GATE_REASONS: frozenset[str] = frozenset(r.value for r in GateReason)


@dataclass(frozen=True)
class GatingResult:
    """Result of a gating check.

    Attributes:
        allowed: Whether the action is allowed.
        reason: The reason for the decision.
        details: Optional dict with additional context (current values, limits, etc.).
    """

    allowed: bool
    reason: GateReason
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON output."""
        return {
            "allowed": self.allowed,
            "reason": self.reason.value,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatingResult:
        """Deserialize from dict."""
        return cls(
            allowed=data["allowed"],
            reason=GateReason(data["reason"]),
            details=data.get("details"),
        )

    @classmethod
    def allow(cls, details: dict[str, Any] | None = None) -> GatingResult:
        """Factory for allowed result."""
        return cls(allowed=True, reason=GateReason.PASS, details=details)

    @classmethod
    def block(cls, reason: GateReason, details: dict[str, Any] | None = None) -> GatingResult:
        """Factory for blocked result."""
        return cls(allowed=False, reason=reason, details=details)
