"""Reconciliation metrics for observability.

See ADR-042 for design decisions.
See ADR-043 for active remediation metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from grinder.reconcile.types import MismatchType

# Metric names (stable contract)
METRIC_MISMATCH_TOTAL = "grinder_reconcile_mismatch_total"
METRIC_LAST_SNAPSHOT_AGE = "grinder_reconcile_last_snapshot_age_seconds"
METRIC_RECONCILE_RUNS = "grinder_reconcile_runs_total"

# LC-10: Remediation metrics
METRIC_ACTION_PLANNED = "grinder_reconcile_action_planned_total"
METRIC_ACTION_EXECUTED = "grinder_reconcile_action_executed_total"
METRIC_ACTION_BLOCKED = "grinder_reconcile_action_blocked_total"

# Label keys
LABEL_TYPE = "type"
LABEL_ACTION = "action"
LABEL_REASON = "reason"

# Known actions for initialization
_KNOWN_ACTIONS = ("cancel_all", "flatten")


@dataclass
class ReconcileMetrics:
    """Metrics for reconciliation.

    Thread-safe via simple dict operations (GIL protection).
    Production-ready for Prometheus export.

    Passive Reconciliation (LC-09b):
        mismatch_counts: {MismatchType.value: count}
        last_snapshot_age_ms: Age of last REST snapshot in ms
        reconcile_runs: Total reconciliation runs

    Active Remediation (LC-10):
        action_planned_counts: {action: count} - dry-run plans
        action_executed_counts: {action: count} - real executions
        action_blocked_counts: {reason: count} - blocked by safety gates
    """

    # Passive reconciliation (LC-09b)
    mismatch_counts: dict[str, int] = field(default_factory=dict)
    last_snapshot_age_ms: int = 0
    reconcile_runs: int = 0

    # Active remediation (LC-10)
    action_planned_counts: dict[str, int] = field(default_factory=dict)
    action_executed_counts: dict[str, int] = field(default_factory=dict)
    action_blocked_counts: dict[str, int] = field(default_factory=dict)

    def record_mismatch(self, mismatch_type: MismatchType) -> None:
        """Record a mismatch event."""
        key = mismatch_type.value
        self.mismatch_counts[key] = self.mismatch_counts.get(key, 0) + 1

    def set_last_snapshot_age(self, age_ms: int) -> None:
        """Set age of last REST snapshot."""
        self.last_snapshot_age_ms = age_ms

    def record_reconcile_run(self) -> None:
        """Record a reconciliation run."""
        self.reconcile_runs += 1

    def record_action_planned(self, action: str) -> None:
        """Record a planned remediation action (dry-run)."""
        self.action_planned_counts[action] = self.action_planned_counts.get(action, 0) + 1

    def record_action_executed(self, action: str) -> None:
        """Record an executed remediation action."""
        self.action_executed_counts[action] = self.action_executed_counts.get(action, 0) + 1

    def record_action_blocked(self, reason: str) -> None:
        """Record a blocked remediation action."""
        self.action_blocked_counts[reason] = self.action_blocked_counts.get(reason, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        """Generate Prometheus text format lines."""
        lines: list[str] = []

        # Mismatch counter
        lines.extend(
            [
                f"# HELP {METRIC_MISMATCH_TOTAL} Total reconciliation mismatches by type",
                f"# TYPE {METRIC_MISMATCH_TOTAL} counter",
            ]
        )

        # Initialize all types to 0 for visibility
        for mtype in MismatchType:
            count = self.mismatch_counts.get(mtype.value, 0)
            lines.append(f'{METRIC_MISMATCH_TOTAL}{{{LABEL_TYPE}="{mtype.value}"}} {count}')

        # Last snapshot age gauge
        lines.extend(
            [
                f"# HELP {METRIC_LAST_SNAPSHOT_AGE} Age of last REST snapshot in seconds",
                f"# TYPE {METRIC_LAST_SNAPSHOT_AGE} gauge",
                f"{METRIC_LAST_SNAPSHOT_AGE} {self.last_snapshot_age_ms / 1000.0:.1f}",
            ]
        )

        # Reconcile runs counter
        lines.extend(
            [
                f"# HELP {METRIC_RECONCILE_RUNS} Total reconciliation runs",
                f"# TYPE {METRIC_RECONCILE_RUNS} counter",
                f"{METRIC_RECONCILE_RUNS} {self.reconcile_runs}",
            ]
        )

        # LC-10: Action planned counter
        lines.extend(
            [
                f"# HELP {METRIC_ACTION_PLANNED} Total remediation actions planned (dry-run)",
                f"# TYPE {METRIC_ACTION_PLANNED} counter",
            ]
        )
        for action in _KNOWN_ACTIONS:
            count = self.action_planned_counts.get(action, 0)
            lines.append(f'{METRIC_ACTION_PLANNED}{{{LABEL_ACTION}="{action}"}} {count}')

        # LC-10: Action executed counter
        lines.extend(
            [
                f"# HELP {METRIC_ACTION_EXECUTED} Total remediation actions executed",
                f"# TYPE {METRIC_ACTION_EXECUTED} counter",
            ]
        )
        for action in _KNOWN_ACTIONS:
            count = self.action_executed_counts.get(action, 0)
            lines.append(f'{METRIC_ACTION_EXECUTED}{{{LABEL_ACTION}="{action}"}} {count}')

        # LC-10: Action blocked counter
        lines.extend(
            [
                f"# HELP {METRIC_ACTION_BLOCKED} Total remediation actions blocked by reason",
                f"# TYPE {METRIC_ACTION_BLOCKED} counter",
            ]
        )
        for reason, count in sorted(self.action_blocked_counts.items()):
            lines.append(f'{METRIC_ACTION_BLOCKED}{{{LABEL_REASON}="{reason}"}} {count}')

        return lines

    def reset(self) -> None:
        """Reset all metrics."""
        self.mismatch_counts.clear()
        self.last_snapshot_age_ms = 0
        self.reconcile_runs = 0
        self.action_planned_counts.clear()
        self.action_executed_counts.clear()
        self.action_blocked_counts.clear()


# Global singleton
_metrics: ReconcileMetrics | None = None


def get_reconcile_metrics() -> ReconcileMetrics:
    """Get or create global reconcile metrics."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = ReconcileMetrics()
    return _metrics


def reset_reconcile_metrics() -> None:
    """Reset reconcile metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
