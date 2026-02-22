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
METRIC_FILL_PROB_BLOCKS = "grinder_router_fill_prob_blocks_total"
METRIC_FILL_PROB_ENFORCE = "grinder_router_fill_prob_enforce_enabled"
METRIC_FILL_PROB_CB_TRIPS = "grinder_router_fill_prob_cb_trips_total"
METRIC_FILL_PROB_AUTO_THRESHOLD = "grinder_router_fill_prob_auto_threshold_bps"
METRIC_FILL_PROB_ENFORCE_ALLOWLIST = "grinder_router_fill_prob_enforce_allowlist_enabled"


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
    fill_prob_blocks: int = 0
    fill_prob_enforce_enabled: bool = False
    fill_prob_cb_trips: int = 0
    fill_prob_auto_threshold_bps: int = 0
    fill_prob_enforce_allowlist_enabled: bool = False

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

    def record_fill_prob_block(self) -> None:
        """Record an order blocked by fill probability gate (PR-C5)."""
        self.fill_prob_blocks += 1

    def set_fill_prob_enforce_enabled(self, enabled: bool) -> None:
        """Set fill probability enforcement state (PR-C5)."""
        self.fill_prob_enforce_enabled = enabled

    def record_cb_trip(self) -> None:
        """Record a fill probability circuit breaker trip (PR-C8)."""
        self.fill_prob_cb_trips += 1

    def set_fill_prob_auto_threshold(self, threshold_bps: int) -> None:
        """Set resolved auto-threshold value (PR-C9). 0 = disabled/failed."""
        self.fill_prob_auto_threshold_bps = threshold_bps

    def set_fill_prob_enforce_allowlist_enabled(self, enabled: bool) -> None:
        """Set whether symbol allowlist is active (PR-C2)."""
        self.fill_prob_enforce_allowlist_enabled = enabled

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

        # Fill probability gate metrics (PR-C5)
        lines.extend(
            [
                f"# HELP {METRIC_FILL_PROB_BLOCKS} Orders blocked by fill probability gate",
                f"# TYPE {METRIC_FILL_PROB_BLOCKS} counter",
                f"{METRIC_FILL_PROB_BLOCKS} {self.fill_prob_blocks}",
                f"# HELP {METRIC_FILL_PROB_ENFORCE} Whether fill probability enforcement is enabled (1=yes, 0=no)",
                f"# TYPE {METRIC_FILL_PROB_ENFORCE} gauge",
                f"{METRIC_FILL_PROB_ENFORCE} {1 if self.fill_prob_enforce_enabled else 0}",
                f"# HELP {METRIC_FILL_PROB_CB_TRIPS} Fill probability circuit breaker trips",
                f"# TYPE {METRIC_FILL_PROB_CB_TRIPS} counter",
                f"{METRIC_FILL_PROB_CB_TRIPS} {self.fill_prob_cb_trips}",
                # PR-C9: Auto-threshold gauge (0 = disabled/failed)
                f"# HELP {METRIC_FILL_PROB_AUTO_THRESHOLD} Resolved auto-threshold from eval report (bps, 0=disabled)",
                f"# TYPE {METRIC_FILL_PROB_AUTO_THRESHOLD} gauge",
                f"{METRIC_FILL_PROB_AUTO_THRESHOLD} {self.fill_prob_auto_threshold_bps}",
                # PR-C2: Symbol allowlist gauge (1 = allowlist active, 0 = all symbols)
                f"# HELP {METRIC_FILL_PROB_ENFORCE_ALLOWLIST} Whether symbol allowlist is active for fill-prob enforcement (1=yes, 0=no)",
                f"# TYPE {METRIC_FILL_PROB_ENFORCE_ALLOWLIST} gauge",
                f"{METRIC_FILL_PROB_ENFORCE_ALLOWLIST} {1 if self.fill_prob_enforce_allowlist_enabled else 0}",
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
