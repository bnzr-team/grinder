"""Cycle layer metrics for Prometheus /metrics endpoint (PR-INV-3b/4 + PR-TP-RENEW).

Tracks TP generation, expiry, renew, replenish, and fill candidate outcomes.
Same singleton pattern as fsm_metrics.py / account/metrics.py.

Metric names:
- grinder_cycle_tp_generated_total{sym}
- grinder_cycle_tp_expired_total{sym}
- grinder_cycle_tp_renew_total{sym, outcome}
- grinder_tp_active_gauge{sym}
- grinder_cycle_fill_candidates_total{sym, outcome}
- grinder_cycle_replenish_generated_total{sym}
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract)
METRIC_CYCLE_TP_GENERATED = "grinder_cycle_tp_generated_total"
METRIC_CYCLE_TP_EXPIRED = "grinder_cycle_tp_expired_total"
METRIC_CYCLE_TP_RENEW = "grinder_cycle_tp_renew_total"
METRIC_TP_ACTIVE_GAUGE = "grinder_tp_active_gauge"
METRIC_CYCLE_FILL_CANDIDATES = "grinder_cycle_fill_candidates_total"
METRIC_CYCLE_REPLENISH_GENERATED = "grinder_cycle_replenish_generated_total"


@dataclass
class CycleMetrics:
    """Metrics collector for LiveCycleLayerV1.

    Thread-safe via simple dict operations (GIL protection).

    Attributes:
        tp_generated: {symbol: count} — TP PLACE actions emitted.
        tp_expired: {symbol: count} — TP CANCEL actions emitted (TTL expiry).
        tp_renew: {(symbol, outcome): count} — TP renew outcomes.
        tp_active: {symbol: 0|1} — whether active TP exists for open position.
        fill_candidates: {(symbol, outcome): count} — fill candidate outcomes.
        replenish_generated: {symbol: count} — replenish PLACE actions emitted (PR-INV-4).
    """

    tp_generated: dict[str, int] = field(default_factory=dict)
    tp_expired: dict[str, int] = field(default_factory=dict)
    tp_renew: dict[tuple[str, str], int] = field(default_factory=dict)
    tp_active: dict[str, int] = field(default_factory=dict)
    fill_candidates: dict[tuple[str, str], int] = field(default_factory=dict)
    replenish_generated: dict[str, int] = field(default_factory=dict)

    def record_tp_generated(self, symbol: str) -> None:
        """Record a TP PLACE action."""
        self.tp_generated[symbol] = self.tp_generated.get(symbol, 0) + 1

    def record_tp_expired(self, symbol: str) -> None:
        """Record a TP expiry CANCEL action."""
        self.tp_expired[symbol] = self.tp_expired.get(symbol, 0) + 1

    def record_tp_renew(self, symbol: str, outcome: str) -> None:
        """Record a TP renew outcome (PR-TP-RENEW).

        Outcomes: started, renewed, failed, cooldown, inflight.
        """
        key = (symbol, outcome)
        self.tp_renew[key] = self.tp_renew.get(key, 0) + 1

    def set_tp_active(self, symbol: str, active: bool) -> None:
        """Set whether an active TP exists for this symbol's position."""
        self.tp_active[symbol] = 1 if active else 0

    def record_replenish_generated(self, symbol: str) -> None:
        """Record a replenish PLACE action (PR-INV-4)."""
        self.replenish_generated[symbol] = self.replenish_generated.get(symbol, 0) + 1

    def record_fill_candidate(self, symbol: str, outcome: str) -> None:
        """Record a fill candidate evaluation outcome.

        Outcomes:
            tp_generated: Grid order filled, TP generated.
            skipped_pending_cancel: Grid order removed by our cancel.
            skipped_tp_order: TP order disappeared (filled/expired).
            skipped_dedup: Duplicate fill (already generated TP).
            skipped_not_ours: Foreign order (not tracked).
        """
        key = (symbol, outcome)
        self.fill_candidates[key] = self.fill_candidates.get(key, 0) + 1

    def format_metrics(self) -> list[str]:
        """Format metrics as Prometheus text exposition lines."""
        lines: list[str] = []

        for symbol, count in sorted(self.tp_generated.items()):
            lines.append(f'{METRIC_CYCLE_TP_GENERATED}{{sym="{symbol}"}} {count}')

        for symbol, count in sorted(self.tp_expired.items()):
            lines.append(f'{METRIC_CYCLE_TP_EXPIRED}{{sym="{symbol}"}} {count}')

        for (symbol, outcome), count in sorted(self.tp_renew.items()):
            lines.append(f'{METRIC_CYCLE_TP_RENEW}{{sym="{symbol}",outcome="{outcome}"}} {count}')

        for symbol, active in sorted(self.tp_active.items()):
            lines.append(f'{METRIC_TP_ACTIVE_GAUGE}{{sym="{symbol}"}} {active}')

        for symbol, count in sorted(self.replenish_generated.items()):
            lines.append(f'{METRIC_CYCLE_REPLENISH_GENERATED}{{sym="{symbol}"}} {count}')

        for (symbol, outcome), count in sorted(self.fill_candidates.items()):
            lines.append(
                f'{METRIC_CYCLE_FILL_CANDIDATES}{{sym="{symbol}",outcome="{outcome}"}} {count}'
            )

        return lines


# Singleton instance
_instance: CycleMetrics | None = None


def get_cycle_metrics() -> CycleMetrics:
    """Get the singleton CycleMetrics instance."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = CycleMetrics()
    return _instance


def reset_cycle_metrics() -> None:
    """Reset the singleton (for testing)."""
    global _instance  # noqa: PLW0603
    _instance = None
