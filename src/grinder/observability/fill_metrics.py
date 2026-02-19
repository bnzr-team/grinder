"""Fill tracking metrics (Launch-06 PR1, health metrics added in PR3).

Provides Prometheus-format counters for fill events:
- grinder_fills_total{source,side,liquidity}
- grinder_fill_notional_total{source,side,liquidity}
- grinder_fill_fees_total{source,side,liquidity}

Health / operational metrics (PR3):
- grinder_fill_ingest_polls_total{source}
- grinder_fill_ingest_enabled{source}
- grinder_fill_ingest_errors_total{source,reason}
- grinder_fill_cursor_load_total{source,result}
- grinder_fill_cursor_save_total{source,result}

Labels use ``source``, ``side``, ``liquidity``, ``reason``, ``result`` only.
No ``symbol=``, ``order_id=``, or other high-cardinality labels.

This module has NO imports from execution/ or connectors/.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract — do not rename without updating metrics_contract.py)
METRIC_FILLS = "grinder_fills_total"
METRIC_FILL_NOTIONAL = "grinder_fill_notional_total"
METRIC_FILL_FEES = "grinder_fill_fees_total"

# Health metric names (PR3)
METRIC_INGEST_POLLS = "grinder_fill_ingest_polls_total"
METRIC_INGEST_ENABLED = "grinder_fill_ingest_enabled"
METRIC_INGEST_ERRORS = "grinder_fill_ingest_errors_total"
METRIC_CURSOR_LOAD = "grinder_fill_cursor_load_total"
METRIC_CURSOR_SAVE = "grinder_fill_cursor_save_total"

# Allowed reason values for ingest errors (no freeform strings)
INGEST_ERROR_REASONS: frozenset[str] = frozenset({"http", "parse", "cursor", "unknown"})

# Allowed result values for cursor ops
CURSOR_RESULTS: frozenset[str] = frozenset({"ok", "error"})


@dataclass
class FillMetrics:
    """Prometheus counters for fill events.

    Thread-safe via simple dict operations (GIL protection).
    """

    fills: dict[tuple[str, str, str], int] = field(default_factory=dict)
    notional: dict[tuple[str, str, str], float] = field(default_factory=dict)
    fees: dict[tuple[str, str, str], float] = field(default_factory=dict)

    # Health counters (PR3) — keyed by (source,)
    ingest_polls: dict[str, int] = field(default_factory=dict)
    ingest_enabled: dict[str, int] = field(default_factory=dict)
    # Keyed by (source, reason)
    ingest_errors: dict[tuple[str, str], int] = field(default_factory=dict)
    # Keyed by (source, result)
    cursor_loads: dict[tuple[str, str], int] = field(default_factory=dict)
    cursor_saves: dict[tuple[str, str], int] = field(default_factory=dict)

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

    def inc_ingest_polls(self, source: str = "reconcile") -> None:
        """Increment ingest polls counter."""
        self.ingest_polls[source] = self.ingest_polls.get(source, 0) + 1

    def set_ingest_enabled(self, source: str, enabled: bool) -> None:
        """Set ingest enabled gauge (1/0)."""
        self.ingest_enabled[source] = 1 if enabled else 0

    def inc_ingest_error(self, source: str, reason: str) -> None:
        """Increment ingest error counter with validated reason."""
        if reason not in INGEST_ERROR_REASONS:
            reason = "unknown"
        key = (source, reason)
        self.ingest_errors[key] = self.ingest_errors.get(key, 0) + 1

    def inc_cursor_load(self, source: str, result: str) -> None:
        """Increment cursor load counter."""
        if result not in CURSOR_RESULTS:
            result = "error"
        key = (source, result)
        self.cursor_loads[key] = self.cursor_loads.get(key, 0) + 1

    def inc_cursor_save(self, source: str, result: str) -> None:
        """Increment cursor save counter."""
        if result not in CURSOR_RESULTS:
            result = "error"
        key = (source, result)
        self.cursor_saves[key] = self.cursor_saves.get(key, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        """Render Prometheus text-format lines."""
        lines: list[str] = []
        lines.extend(self._render_fill_counters())
        lines.extend(self._render_health_metrics())
        return lines

    def _render_fill_counters(self) -> list[str]:
        """Render fill event counters (PR1)."""
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

    def _render_health_metrics(self) -> list[str]:  # noqa: PLR0912
        """Render health / operational metrics (PR3)."""
        lines: list[str] = []

        # --- ingest polls ---
        lines.append(f"# HELP {METRIC_INGEST_POLLS} Total fill ingest poll iterations")
        lines.append(f"# TYPE {METRIC_INGEST_POLLS} counter")
        if self.ingest_polls:
            for source, count in sorted(self.ingest_polls.items()):
                lines.append(f'{METRIC_INGEST_POLLS}{{source="{source}"}} {count}')
        else:
            lines.append(f'{METRIC_INGEST_POLLS}{{source="none"}} 0')

        # --- ingest enabled gauge ---
        lines.append(f"# HELP {METRIC_INGEST_ENABLED} Whether fill ingest is enabled (1/0)")
        lines.append(f"# TYPE {METRIC_INGEST_ENABLED} gauge")
        if self.ingest_enabled:
            for source, val in sorted(self.ingest_enabled.items()):
                lines.append(f'{METRIC_INGEST_ENABLED}{{source="{source}"}} {val}')
        else:
            lines.append(f'{METRIC_INGEST_ENABLED}{{source="none"}} 0')

        # --- ingest errors ---
        lines.append(f"# HELP {METRIC_INGEST_ERRORS} Total fill ingest errors by source and reason")
        lines.append(f"# TYPE {METRIC_INGEST_ERRORS} counter")
        if self.ingest_errors:
            for (source, reason), count in sorted(self.ingest_errors.items()):
                lines.append(
                    f'{METRIC_INGEST_ERRORS}{{source="{source}",reason="{reason}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_INGEST_ERRORS}{{source="none",reason="none"}} 0')

        # --- cursor load ---
        lines.append(f"# HELP {METRIC_CURSOR_LOAD} Total fill cursor load attempts by result")
        lines.append(f"# TYPE {METRIC_CURSOR_LOAD} counter")
        if self.cursor_loads:
            for (source, result), count in sorted(self.cursor_loads.items()):
                lines.append(f'{METRIC_CURSOR_LOAD}{{source="{source}",result="{result}"}} {count}')
        else:
            lines.append(f'{METRIC_CURSOR_LOAD}{{source="none",result="none"}} 0')

        # --- cursor save ---
        lines.append(f"# HELP {METRIC_CURSOR_SAVE} Total fill cursor save attempts by result")
        lines.append(f"# TYPE {METRIC_CURSOR_SAVE} counter")
        if self.cursor_saves:
            for (source, result), count in sorted(self.cursor_saves.items()):
                lines.append(f'{METRIC_CURSOR_SAVE}{{source="{source}",result="{result}"}} {count}')
        else:
            lines.append(f'{METRIC_CURSOR_SAVE}{{source="none",result="none"}} 0')

        return lines

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        self.fills.clear()
        self.notional.clear()
        self.fees.clear()
        self.ingest_polls.clear()
        self.ingest_enabled.clear()
        self.ingest_errors.clear()
        self.cursor_loads.clear()
        self.cursor_saves.clear()


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
