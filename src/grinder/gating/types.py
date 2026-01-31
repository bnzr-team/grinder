"""Gating types and contracts.

Defines the result types for gating decisions (rate limit, risk checks).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class GateReason(Enum):
    """Reason for gating decision."""

    PASS = "PASS"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    MAX_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXCEEDED"
    DAILY_LOSS_LIMIT_EXCEEDED = "DAILY_LOSS_LIMIT_EXCEEDED"
    MAX_ORDERS_EXCEEDED = "MAX_ORDERS_EXCEEDED"


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
