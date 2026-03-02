"""Account sync metrics for Prometheus /metrics endpoint (Launch-15).

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.7)

Design:
- Pure dataclass singleton (same pattern as execution/sor_metrics.py)
- Thread-safe via dict operations (GIL-protected)
- No external dependencies
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Metric names (stable contract)
METRIC_SYNC_LAST_TS = "grinder_account_sync_last_ts"
METRIC_SYNC_AGE_SECONDS = "grinder_account_sync_age_seconds"
METRIC_SYNC_ERRORS = "grinder_account_sync_errors_total"
METRIC_SYNC_MISMATCHES = "grinder_account_sync_mismatches_total"
METRIC_SYNC_POSITIONS_COUNT = "grinder_account_sync_positions_count"
METRIC_SYNC_OPEN_ORDERS_COUNT = "grinder_account_sync_open_orders_count"
METRIC_SYNC_PENDING_NOTIONAL = "grinder_account_sync_pending_notional"


@dataclass
class AccountSyncMetrics:
    """Metrics collector for AccountSyncer.

    Thread-safe via simple dict operations (GIL protection).
    Production-ready for Prometheus export.

    Attributes:
        last_sync_ts: Unix ms of last successful sync.
        sync_errors: {reason: count} counter.
        mismatches: {rule: count} counter.
        positions_count: Number of positions in last snapshot.
        open_orders_count: Number of open orders in last snapshot.
        pending_notional: Total notional of open orders (price * remaining_qty).
    """

    last_sync_ts: int = 0
    sync_errors: dict[str, int] = field(default_factory=dict)
    mismatches: dict[str, int] = field(default_factory=dict)
    positions_count: int = 0
    open_orders_count: int = 0
    pending_notional: float = 0.0

    def record_sync(
        self, ts: int, positions: int, open_orders: int, pending_notional: float
    ) -> None:
        """Record a successful sync.

        When ts=0 (empty account — no positions/orders to derive a timestamp
        from), falls back to wall-clock ms so that last_sync_ts always reflects
        "a sync happened" rather than "never synced".
        """
        self.last_sync_ts = ts if ts > 0 else int(time.time() * 1000)
        self.positions_count = positions
        self.open_orders_count = open_orders
        self.pending_notional = pending_notional

    def record_error(self, reason: str) -> None:
        """Record a sync error."""
        self.sync_errors[reason] = self.sync_errors.get(reason, 0) + 1

    def record_mismatch(self, rule: str) -> None:
        """Record a mismatch detection."""
        self.mismatches[rule] = self.mismatches.get(rule, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        """Generate Prometheus text format lines."""
        lines: list[str] = []

        # Last sync timestamp
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_LAST_TS} Unix ms of last successful account sync",
                f"# TYPE {METRIC_SYNC_LAST_TS} gauge",
                f"{METRIC_SYNC_LAST_TS} {self.last_sync_ts}",
            ]
        )

        # Age since last sync
        age = time.time() - self.last_sync_ts / 1000.0 if self.last_sync_ts > 0 else 0.0
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_AGE_SECONDS} Seconds since last successful account sync",
                f"# TYPE {METRIC_SYNC_AGE_SECONDS} gauge",
                f"{METRIC_SYNC_AGE_SECONDS} {age:.2f}",
            ]
        )

        # Sync errors counter
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_ERRORS} Account sync errors by reason",
                f"# TYPE {METRIC_SYNC_ERRORS} counter",
            ]
        )
        if self.sync_errors:
            for reason, count in sorted(self.sync_errors.items()):
                lines.append(f'{METRIC_SYNC_ERRORS}{{reason="{reason}"}} {count}')
        else:
            lines.append(f'{METRIC_SYNC_ERRORS}{{reason="none"}} 0')

        # Mismatches counter
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_MISMATCHES} Account sync mismatches by rule",
                f"# TYPE {METRIC_SYNC_MISMATCHES} counter",
            ]
        )
        if self.mismatches:
            for rule, count in sorted(self.mismatches.items()):
                lines.append(f'{METRIC_SYNC_MISMATCHES}{{rule="{rule}"}} {count}')
        else:
            lines.append(f'{METRIC_SYNC_MISMATCHES}{{rule="none"}} 0')

        # Positions count gauge
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_POSITIONS_COUNT} Number of positions in last account snapshot",
                f"# TYPE {METRIC_SYNC_POSITIONS_COUNT} gauge",
                f"{METRIC_SYNC_POSITIONS_COUNT} {self.positions_count}",
            ]
        )

        # Open orders count gauge
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_OPEN_ORDERS_COUNT} Number of open orders in last account snapshot",
                f"# TYPE {METRIC_SYNC_OPEN_ORDERS_COUNT} gauge",
                f"{METRIC_SYNC_OPEN_ORDERS_COUNT} {self.open_orders_count}",
            ]
        )

        # Pending notional gauge
        lines.extend(
            [
                f"# HELP {METRIC_SYNC_PENDING_NOTIONAL} Total notional value of open orders",
                f"# TYPE {METRIC_SYNC_PENDING_NOTIONAL} gauge",
                f"{METRIC_SYNC_PENDING_NOTIONAL} {self.pending_notional:.2f}",
            ]
        )

        return lines


# Global singleton
_metrics: AccountSyncMetrics | None = None


def get_account_sync_metrics() -> AccountSyncMetrics:
    """Get or create global account sync metrics instance."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = AccountSyncMetrics()
    return _metrics


def reset_account_sync_metrics() -> None:
    """Reset account sync metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
