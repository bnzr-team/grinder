"""Fill tracking metrics (Launch-06 PR1).

Provides Prometheus-format counters for fill events:
- grinder_fills_total{source,side,liquidity}
- grinder_fill_notional_total{source,side,liquidity}
- grinder_fill_fees_total{source,side,liquidity}

Labels use ``source``, ``side``, ``liquidity`` only.
No ``symbol=``, ``order_id=``, or other high-cardinality labels.

This module has NO imports from execution/ or connectors/.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract — do not rename without updating metrics_contract.py)
METRIC_FILLS = "grinder_fills_total"
METRIC_FILL_NOTIONAL = "grinder_fill_notional_total"
METRIC_FILL_FEES = "grinder_fill_fees_total"


@dataclass
class FillMetrics:
    """Prometheus counters for fill events.

    Thread-safe via simple dict operations (GIL protection).
    """

    fills: dict[tuple[str, str, str], int] = field(default_factory=dict)
    notional: dict[tuple[str, str, str], float] = field(default_factory=dict)
    fees: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def record_fill(
        self,
        source: str,
        side: str,
        liquidity: str,
        notional_value: float,
        fee: float,
    ) -> None:
        """Record a fill event with labels."""
        key = (source, side, liquidity)
        self.fills[key] = self.fills.get(key, 0) + 1
        self.notional[key] = self.notional.get(key, 0.0) + notional_value
        self.fees[key] = self.fees.get(key, 0.0) + fee

    def to_prometheus_lines(self) -> list[str]:
        """Render Prometheus text-format lines."""
        lines: list[str] = []

        # --- fills ---
        lines.append(f"# HELP {METRIC_FILLS} Total fill events by source, side, and liquidity")
        lines.append(f"# TYPE {METRIC_FILLS} counter")
        if self.fills:
            for (source, side, liq), count in sorted(self.fills.items()):
                lines.append(
                    f'{METRIC_FILLS}{{source="{source}",side="{side}",liquidity="{liq}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_FILLS}{{source="none",side="none",liquidity="none"}} 0')

        # --- notional ---
        lines.append(
            f"# HELP {METRIC_FILL_NOTIONAL} Total fill notional value by source, side, and liquidity"
        )
        lines.append(f"# TYPE {METRIC_FILL_NOTIONAL} counter")
        if self.notional:
            for (source, side, liq), value in sorted(self.notional.items()):
                lines.append(
                    f'{METRIC_FILL_NOTIONAL}{{source="{source}",side="{side}",liquidity="{liq}"}} {value}'
                )
        else:
            lines.append(f'{METRIC_FILL_NOTIONAL}{{source="none",side="none",liquidity="none"}} 0')

        # --- fees ---
        lines.append(f"# HELP {METRIC_FILL_FEES} Total fill fees by source, side, and liquidity")
        lines.append(f"# TYPE {METRIC_FILL_FEES} counter")
        if self.fees:
            for (source, side, liq), value in sorted(self.fees.items()):
                lines.append(
                    f'{METRIC_FILL_FEES}{{source="{source}",side="{side}",liquidity="{liq}"}} {value}'
                )
        else:
            lines.append(f'{METRIC_FILL_FEES}{{source="none",side="none",liquidity="none"}} 0')

        return lines

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        self.fills.clear()
        self.notional.clear()
        self.fees.clear()


# Global singleton
_metrics: FillMetrics | None = None


def get_fill_metrics() -> FillMetrics:
    """Get or create global fill metrics."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = FillMetrics()
    return _metrics


def reset_fill_metrics() -> None:
    """Reset fill metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
