"""SOR (SmartOrderRouter) metrics for Prometheus /metrics endpoint.

Launch-14 PR2: Runtime observability for router decisions.
SSOT: docs/14_SMART_ORDER_ROUTER_SPEC.md (Sec 14.9).

Design:
- Pure dataclass singleton (same pattern as live/fsm_metrics.py)
- Thread-safe via dict operations (GIL-protected)
- No external dependencies
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract)
METRIC_ROUTER_DECISION = "grinder_router_decision_total"
METRIC_ROUTER_AMEND_SAVINGS = "grinder_router_amend_savings_total"


@dataclass
class SorMetrics:
    """Metrics collector for SmartOrderRouter decisions.

    Thread-safe via simple dict operations (GIL protection).
    Production-ready for Prometheus export.

    Attributes:
        decisions: {(decision, reason): count} counter
        amend_savings: Count of amend decisions that saved a cancel+place
    """

    decisions: dict[tuple[str, str], int] = field(default_factory=dict)
    amend_savings: int = 0

    def record_decision(self, decision: str, reason: str) -> None:
        """Record a router decision.

        Args:
            decision: RouterDecision value (e.g. "CANCEL_REPLACE", "NOOP", "BLOCK").
            reason: Machine-readable reason code.
        """
        key = (decision, reason)
        self.decisions[key] = self.decisions.get(key, 0) + 1

    def record_amend_saving(self) -> None:
        """Record an amend that saved a cancel+place pair."""
        self.amend_savings += 1

    def to_prometheus_lines(self) -> list[str]:
        """Generate Prometheus text format lines.

        Returns:
            List of lines in Prometheus exposition format.
        """
        lines: list[str] = []

        # Router decision counter
        lines.extend(
            [
                f"# HELP {METRIC_ROUTER_DECISION} Router decisions by type and reason",
                f"# TYPE {METRIC_ROUTER_DECISION} counter",
            ]
        )
        if self.decisions:
            for (decision, reason), count in sorted(self.decisions.items()):
                lines.append(
                    f'{METRIC_ROUTER_DECISION}{{decision="{decision}",reason="{reason}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_ROUTER_DECISION}{{decision="none",reason="none"}} 0')

        # Amend savings counter
        lines.extend(
            [
                f"# HELP {METRIC_ROUTER_AMEND_SAVINGS} Amends that saved a cancel+place pair",
                f"# TYPE {METRIC_ROUTER_AMEND_SAVINGS} counter",
                f"{METRIC_ROUTER_AMEND_SAVINGS} {self.amend_savings}",
            ]
        )

        return lines


# Global singleton
_metrics: SorMetrics | None = None


def get_sor_metrics() -> SorMetrics:
    """Get or create global SOR metrics instance."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = SorMetrics()
    return _metrics


def reset_sor_metrics() -> None:
    """Reset SOR metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
