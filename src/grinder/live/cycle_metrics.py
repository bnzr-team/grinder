"""Cycle layer metrics for Prometheus /metrics endpoint (PR-INV-3b/4).

Tracks TP generation, expiry, replenish, and fill candidate outcomes.
Same singleton pattern as fsm_metrics.py / account/metrics.py.

Metric names:
- grinder_cycle_tp_generated_total{symbol}
- grinder_cycle_tp_expired_total{symbol}
- grinder_cycle_fill_candidates_total{symbol, outcome}
- grinder_cycle_replenish_generated_total{symbol}
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract)
METRIC_CYCLE_TP_GENERATED = "grinder_cycle_tp_generated_total"
METRIC_CYCLE_TP_EXPIRED = "grinder_cycle_tp_expired_total"
METRIC_CYCLE_FILL_CANDIDATES = "grinder_cycle_fill_candidates_total"
METRIC_CYCLE_REPLENISH_GENERATED = "grinder_cycle_replenish_generated_total"


@dataclass
class CycleMetrics:
    """Metrics collector for LiveCycleLayerV1.

    Thread-safe via simple dict operations (GIL protection).

    Attributes:
        tp_generated: {symbol: count} — TP PLACE actions emitted.
        tp_expired: {symbol: count} — TP CANCEL actions emitted (TTL expiry).
        fill_candidates: {(symbol, outcome): count} — fill candidate outcomes.
        replenish_generated: {symbol: count} — replenish PLACE actions emitted (PR-INV-4).
    """

    tp_generated: dict[str, int] = field(default_factory=dict)
    tp_expired: dict[str, int] = field(default_factory=dict)
    fill_candidates: dict[tuple[str, str], int] = field(default_factory=dict)
    replenish_generated: dict[str, int] = field(default_factory=dict)

    def record_tp_generated(self, symbol: str) -> None:
        """Record a TP PLACE action."""
        self.tp_generated[symbol] = self.tp_generated.get(symbol, 0) + 1

    def record_tp_expired(self, symbol: str) -> None:
        """Record a TP expiry CANCEL action."""
        self.tp_expired[symbol] = self.tp_expired.get(symbol, 0) + 1

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
            lines.append(f'{METRIC_CYCLE_TP_GENERATED}{{symbol="{symbol}"}} {count}')

        for symbol, count in sorted(self.tp_expired.items()):
            lines.append(f'{METRIC_CYCLE_TP_EXPIRED}{{symbol="{symbol}"}} {count}')

        for symbol, count in sorted(self.replenish_generated.items()):
            lines.append(f'{METRIC_CYCLE_REPLENISH_GENERATED}{{symbol="{symbol}"}} {count}')

        for (symbol, outcome), count in sorted(self.fill_candidates.items()):
            lines.append(
                f'{METRIC_CYCLE_FILL_CANDIDATES}{{symbol="{symbol}",outcome="{outcome}"}} {count}'
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
