"""Live engine metrics for Prometheus /metrics endpoint (PR-ROLL-1).

Tracks reduce_only enforcement events when position is open.
Same singleton pattern as cycle_metrics.py / fsm_metrics.py.

Metric names:
- grinder_live_reduce_only_enforced_total{sym,side,reason}
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract)
METRIC_REDUCE_ONLY_ENFORCED = "grinder_live_reduce_only_enforced_total"


@dataclass
class LiveEngineMetrics:
    """Metrics collector for LiveEngineV0 safety enforcement.

    Thread-safe via simple dict operations (GIL protection).

    Attributes:
        reduce_only_enforced: {(sym, side, reason): count} — enforcement events.
    """

    reduce_only_enforced: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def record_reduce_only_enforced(self, symbol: str, side: str, reason: str) -> None:
        """Record a reduce_only enforcement event.

        Args:
            symbol: Trading pair (e.g. "BTCUSDT").
            side: Order side that was enforced ("BUY" or "SELL").
            reason: Why enforcement was applied ("position_long" or "position_short").
        """
        key = (symbol, side, reason)
        self.reduce_only_enforced[key] = self.reduce_only_enforced.get(key, 0) + 1

    def format_metrics(self) -> list[str]:
        """Format metrics as Prometheus text exposition lines."""
        lines: list[str] = []

        lines.append(
            f"# HELP {METRIC_REDUCE_ONLY_ENFORCED}"
            " Opposite-side orders forced reduce_only when position open"
        )
        lines.append(f"# TYPE {METRIC_REDUCE_ONLY_ENFORCED} counter")

        if self.reduce_only_enforced:
            for (sym, side, reason), count in sorted(self.reduce_only_enforced.items()):
                lines.append(
                    f"{METRIC_REDUCE_ONLY_ENFORCED}"
                    f'{{sym="{sym}",side="{side}",reason="{reason}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_REDUCE_ONLY_ENFORCED}{{sym="none",side="none",reason="none"}} 0')

        return lines


# Singleton instance
_instance: LiveEngineMetrics | None = None


def get_live_engine_metrics() -> LiveEngineMetrics:
    """Get the singleton LiveEngineMetrics instance."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = LiveEngineMetrics()
    return _instance


def reset_live_engine_metrics() -> None:
    """Reset the singleton (for testing)."""
    global _instance  # noqa: PLW0603
    _instance = None
