"""Reconciliation metrics for observability.

See ADR-042 for design decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from grinder.reconcile.types import MismatchType

# Metric names (stable contract)
METRIC_MISMATCH_TOTAL = "grinder_reconcile_mismatch_total"
METRIC_LAST_SNAPSHOT_AGE = "grinder_reconcile_last_snapshot_age_seconds"
METRIC_RECONCILE_RUNS = "grinder_reconcile_runs_total"

# Label keys
LABEL_TYPE = "type"


@dataclass
class ReconcileMetrics:
    """Metrics for reconciliation.

    Thread-safe via simple dict operations (GIL protection).
    Production-ready for Prometheus export.

    Attributes:
        mismatch_counts: {MismatchType.value: count}
        last_snapshot_age_ms: Age of last REST snapshot in ms
        reconcile_runs: Total reconciliation runs
    """

    mismatch_counts: dict[str, int] = field(default_factory=dict)
    last_snapshot_age_ms: int = 0
    reconcile_runs: int = 0

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

        return lines

    def reset(self) -> None:
        """Reset all metrics."""
        self.mismatch_counts.clear()
        self.last_snapshot_age_ms = 0
        self.reconcile_runs = 0


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
