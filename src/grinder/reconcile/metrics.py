"""Reconciliation metrics for observability.

See ADR-042 for design decisions.
See ADR-043 for active remediation metrics.
See ADR-044 for runner wiring metrics.
See ADR-046 for budget metrics (LC-18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from grinder.reconcile.types import MismatchType

# Metric names (stable contract)
METRIC_MISMATCH_TOTAL = "grinder_reconcile_mismatch_total"
METRIC_LAST_SNAPSHOT_AGE = "grinder_reconcile_last_snapshot_age_seconds"
METRIC_LAST_SNAPSHOT_TS = "grinder_reconcile_last_snapshot_ts_ms"
METRIC_RECONCILE_RUNS = "grinder_reconcile_runs_total"

# LC-10: Remediation metrics
METRIC_ACTION_PLANNED = "grinder_reconcile_action_planned_total"
METRIC_ACTION_EXECUTED = "grinder_reconcile_action_executed_total"
METRIC_ACTION_BLOCKED = "grinder_reconcile_action_blocked_total"

# LC-11: Runner wiring metrics
METRIC_RUNS_WITH_MISMATCH = "grinder_reconcile_runs_with_mismatch_total"
METRIC_RUNS_WITH_REMEDIATION = "grinder_reconcile_runs_with_remediation_total"
METRIC_LAST_REMEDIATION_TS = "grinder_reconcile_last_remediation_ts_ms"

# LC-18: Budget metrics
METRIC_BUDGET_CALLS_USED_DAY = "grinder_reconcile_budget_calls_used_day"
METRIC_BUDGET_NOTIONAL_USED_DAY = "grinder_reconcile_budget_notional_used_day"
METRIC_BUDGET_CALLS_REMAINING_DAY = "grinder_reconcile_budget_calls_remaining_day"
METRIC_BUDGET_NOTIONAL_REMAINING_DAY = "grinder_reconcile_budget_notional_remaining_day"
METRIC_BUDGET_CONFIGURED = "grinder_reconcile_budget_configured"

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

    Runner Wiring (LC-11):
        runs_with_mismatch: Total runs that detected at least one mismatch
        runs_with_remediation_counts: {action: count} - runs with executed actions
        last_remediation_ts_ms: Timestamp of last remediation action

    Budget (LC-18):
        budget_calls_used_day: Remediation calls used today
        budget_notional_used_day: Notional USDT used today
        budget_calls_remaining_day: Remaining calls for today
        budget_notional_remaining_day: Remaining notional for today
    """

    # Passive reconciliation (LC-09b)
    mismatch_counts: dict[str, int] = field(default_factory=dict)
    last_snapshot_age_ms: int = 0
    last_snapshot_ts_ms: int = 0  # 0 = never taken
    reconcile_runs: int = 0

    # Active remediation (LC-10)
    action_planned_counts: dict[str, int] = field(default_factory=dict)
    action_executed_counts: dict[str, int] = field(default_factory=dict)
    action_blocked_counts: dict[str, int] = field(default_factory=dict)

    # Runner wiring (LC-11)
    runs_with_mismatch: int = 0
    runs_with_remediation_counts: dict[str, int] = field(default_factory=dict)
    last_remediation_ts_ms: int = 0

    # -- Budget (LC-18) --
    budget_calls_used_day: int = 0
    budget_notional_used_day: Decimal = field(default_factory=lambda: Decimal("0"))
    budget_calls_remaining_day: int = 0
    budget_notional_remaining_day: Decimal = field(default_factory=lambda: Decimal("0"))
    budget_configured: bool = False  # True when budget tracker is active

    def record_mismatch(self, mismatch_type: MismatchType) -> None:
        """Record a mismatch event."""
        key = mismatch_type.value
        self.mismatch_counts[key] = self.mismatch_counts.get(key, 0) + 1

    def set_last_snapshot_age(self, age_ms: int) -> None:
        """Set age of last REST snapshot."""
        self.last_snapshot_age_ms = age_ms

    def set_last_snapshot_ts(self, ts_ms: int) -> None:
        """Set timestamp of last REST snapshot (0 = never taken)."""
        self.last_snapshot_ts_ms = ts_ms

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

    def record_run_with_mismatch(self) -> None:
        """Record a run that detected at least one mismatch."""
        self.runs_with_mismatch += 1

    def record_run_with_remediation(self, action: str) -> None:
        """Record a run that executed at least one remediation action."""
        self.runs_with_remediation_counts[action] = (
            self.runs_with_remediation_counts.get(action, 0) + 1
        )

    def set_last_remediation_ts(self, ts_ms: int) -> None:
        """Set timestamp of last remediation action."""
        self.last_remediation_ts_ms = ts_ms

    def set_budget_metrics(
        self,
        calls_used: int,
        notional_used: Decimal,
        calls_remaining: int,
        notional_remaining: Decimal,
        configured: bool = True,
    ) -> None:
        """Set budget metrics from BudgetTracker state (LC-18).

        Args:
            calls_used: Remediation calls used today
            notional_used: Notional USDT used today
            calls_remaining: Remaining calls for today
            notional_remaining: Remaining notional for today
            configured: Whether budget tracking is active (default True)
        """
        self.budget_calls_used_day = calls_used
        self.budget_notional_used_day = notional_used
        self.budget_calls_remaining_day = calls_remaining
        self.budget_notional_remaining_day = notional_remaining
        self.budget_configured = configured

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

        # Last snapshot timestamp gauge (0 = never taken)
        lines.extend(
            [
                f"# HELP {METRIC_LAST_SNAPSHOT_TS} Timestamp of last REST snapshot in ms (0=never)",
                f"# TYPE {METRIC_LAST_SNAPSHOT_TS} gauge",
                f"{METRIC_LAST_SNAPSHOT_TS} {self.last_snapshot_ts_ms}",
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

        # LC-11: Runs with mismatch counter
        lines.extend(
            [
                f"# HELP {METRIC_RUNS_WITH_MISMATCH} Total runs that detected mismatches",
                f"# TYPE {METRIC_RUNS_WITH_MISMATCH} counter",
                f"{METRIC_RUNS_WITH_MISMATCH} {self.runs_with_mismatch}",
            ]
        )

        # LC-11: Runs with remediation counter
        lines.extend(
            [
                f"# HELP {METRIC_RUNS_WITH_REMEDIATION} Total runs that executed remediation",
                f"# TYPE {METRIC_RUNS_WITH_REMEDIATION} counter",
            ]
        )
        for action in _KNOWN_ACTIONS:
            count = self.runs_with_remediation_counts.get(action, 0)
            lines.append(f'{METRIC_RUNS_WITH_REMEDIATION}{{{LABEL_ACTION}="{action}"}} {count}')

        # LC-11: Last remediation timestamp gauge
        lines.extend(
            [
                f"# HELP {METRIC_LAST_REMEDIATION_TS} Timestamp of last remediation action (ms)",
                f"# TYPE {METRIC_LAST_REMEDIATION_TS} gauge",
                f"{METRIC_LAST_REMEDIATION_TS} {self.last_remediation_ts_ms}",
            ]
        )

        # LC-18: Budget metrics
        lines.extend(
            [
                f"# HELP {METRIC_BUDGET_CALLS_USED_DAY} Remediation calls used today",
                f"# TYPE {METRIC_BUDGET_CALLS_USED_DAY} gauge",
                f"{METRIC_BUDGET_CALLS_USED_DAY} {self.budget_calls_used_day}",
            ]
        )

        lines.extend(
            [
                f"# HELP {METRIC_BUDGET_NOTIONAL_USED_DAY} Notional USDT used today",
                f"# TYPE {METRIC_BUDGET_NOTIONAL_USED_DAY} gauge",
                f"{METRIC_BUDGET_NOTIONAL_USED_DAY} {float(self.budget_notional_used_day):.2f}",
            ]
        )

        lines.extend(
            [
                f"# HELP {METRIC_BUDGET_CALLS_REMAINING_DAY} Remediation calls remaining today",
                f"# TYPE {METRIC_BUDGET_CALLS_REMAINING_DAY} gauge",
                f"{METRIC_BUDGET_CALLS_REMAINING_DAY} {self.budget_calls_remaining_day}",
            ]
        )

        lines.extend(
            [
                f"# HELP {METRIC_BUDGET_NOTIONAL_REMAINING_DAY} Notional USDT remaining today",
                f"# TYPE {METRIC_BUDGET_NOTIONAL_REMAINING_DAY} gauge",
                f"{METRIC_BUDGET_NOTIONAL_REMAINING_DAY} {float(self.budget_notional_remaining_day):.2f}",
            ]
        )

        # LC-18: Budget configured gauge (1=active, 0=not configured)
        lines.extend(
            [
                f"# HELP {METRIC_BUDGET_CONFIGURED} Whether budget tracking is active (1=yes, 0=no)",
                f"# TYPE {METRIC_BUDGET_CONFIGURED} gauge",
                f"{METRIC_BUDGET_CONFIGURED} {1 if self.budget_configured else 0}",
            ]
        )

        return lines

    def reset(self) -> None:
        """Reset all metrics."""
        self.mismatch_counts.clear()
        self.last_snapshot_age_ms = 0
        self.reconcile_runs = 0
        self.action_planned_counts.clear()
        self.action_executed_counts.clear()
        self.action_blocked_counts.clear()
        self.runs_with_mismatch = 0
        self.runs_with_remediation_counts.clear()
        self.last_remediation_ts_ms = 0
        # -- LC-18: Budget metrics --
        self.budget_calls_used_day = 0
        self.budget_notional_used_day = Decimal("0")
        self.budget_calls_remaining_day = 0
        self.budget_notional_remaining_day = Decimal("0")
        self.budget_configured = False


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
