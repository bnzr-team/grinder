"""Gating metrics for observability.

Metrics exported:
- grinder_gating_allowed_total{gate}: Counter of allowed gating decisions
- grinder_gating_blocked_total{gate,reason}: Counter of blocked gating decisions

These metric names and label keys are stable contracts.
DO NOT rename without updating metric contracts and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from grinder.gating.types import GateName, GateReason

# Metric name constants (stable contracts)
METRIC_GATING_ALLOWED = "grinder_gating_allowed_total"
METRIC_GATING_BLOCKED = "grinder_gating_blocked_total"

# Label key constants (stable contracts)
LABEL_GATE = "gate"
LABEL_REASON = "reason"


@dataclass
class GatingMetrics:
    """Gating metrics collector.

    Tracks allowed/blocked decisions by gate and reason.
    In production, this would use prometheus_client or similar.
    """

    allowed_total: dict[str, int] = field(default_factory=dict)
    """Allowed counter by gate name."""

    blocked_total: dict[tuple[str, str], int] = field(default_factory=dict)
    """Blocked counter by (gate_name, reason) tuple."""

    def record_allowed(self, gate: GateName) -> None:
        """Record an allowed gating decision.

        Args:
            gate: Which gate made the decision.
        """
        key = gate.value
        self.allowed_total[key] = self.allowed_total.get(key, 0) + 1

    def record_blocked(self, gate: GateName, reason: GateReason) -> None:
        """Record a blocked gating decision.

        Args:
            gate: Which gate made the decision.
            reason: Why the request was blocked.
        """
        key = (gate.value, reason.value)
        self.blocked_total[key] = self.blocked_total.get(key, 0) + 1

    def get_allowed_count(self, gate: GateName) -> int:
        """Get allowed count for a gate."""
        return self.allowed_total.get(gate.value, 0)

    def get_blocked_count(self, gate: GateName, reason: GateReason | None = None) -> int:
        """Get blocked count for a gate, optionally filtered by reason."""
        if reason is not None:
            return self.blocked_total.get((gate.value, reason.value), 0)
        # Sum all reasons for this gate
        return sum(count for (g, _), count in self.blocked_total.items() if g == gate.value)

    def get_metrics(self) -> dict[str, Any]:
        """Get all metrics as dict (Prometheus-compatible structure).

        Returns a dict with metric names as keys and label->value mappings.
        """
        allowed = {
            f"{{{LABEL_GATE}={gate!r}}}": count for gate, count in self.allowed_total.items()
        }
        blocked = {
            f"{{{LABEL_GATE}={gate!r},{LABEL_REASON}={reason!r}}}": count
            for (gate, reason), count in self.blocked_total.items()
        }
        return {
            METRIC_GATING_ALLOWED: allowed,
            METRIC_GATING_BLOCKED: blocked,
        }

    def to_prometheus_lines(self) -> list[str]:
        """Export metrics in Prometheus text format.

        Returns list of lines suitable for /metrics endpoint.
        """
        lines: list[str] = []

        # HELP and TYPE for allowed
        lines.append(f"# HELP {METRIC_GATING_ALLOWED} Total allowed gating decisions")
        lines.append(f"# TYPE {METRIC_GATING_ALLOWED} counter")
        for gate, count in sorted(self.allowed_total.items()):
            lines.append(f'{METRIC_GATING_ALLOWED}{{{LABEL_GATE}="{gate}"}} {count}')

        # HELP and TYPE for blocked
        lines.append(f"# HELP {METRIC_GATING_BLOCKED} Total blocked gating decisions")
        lines.append(f"# TYPE {METRIC_GATING_BLOCKED} counter")
        for (gate, reason), count in sorted(self.blocked_total.items()):
            lines.append(
                f'{METRIC_GATING_BLOCKED}{{{LABEL_GATE}="{gate}",{LABEL_REASON}="{reason}"}} {count}'
            )

        return lines

    def initialize_zero_series(self) -> None:
        """Pre-populate zero-value series for all known gates.

        Ensures Prometheus scrapes show 0-value series immediately,
        before any gating decisions are made. Idempotent: does not
        reset already-incremented counters.
        """
        for gate in GateName:
            if gate.value not in self.allowed_total:
                self.allowed_total[gate.value] = 0

    def reset(self) -> None:
        """Reset all metrics."""
        self.allowed_total.clear()
        self.blocked_total.clear()


# Global metrics instance
_metrics = GatingMetrics()


def get_gating_metrics() -> GatingMetrics:
    """Get global gating metrics instance."""
    return _metrics


def reset_gating_metrics() -> None:
    """Reset global gating metrics."""
    _metrics.reset()
