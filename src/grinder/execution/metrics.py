"""Execution metrics for observability.

Minimal metrics set for M1 vertical slice:
- grinder_orders_open{symbol,side}: Current open orders
- grinder_intents_total{type}: Total intents generated
- grinder_exec_events_total{type}: Total execution events
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from grinder.core import OrderSide  # noqa: TC001 - used at runtime (.value)
from grinder.execution.types import ActionType, ExecutionEvent  # noqa: TC001 - used at runtime


@dataclass
class ExecutionMetrics:
    """Execution metrics collector.

    This is a simple in-memory metrics collector for M1.
    In production, this would use prometheus_client or similar.
    """

    # Counters
    intents_total: dict[str, int] = field(default_factory=dict)
    exec_events_total: dict[str, int] = field(default_factory=dict)

    # Gauges
    orders_open: dict[tuple[str, str], int] = field(default_factory=dict)

    def record_intent(self, action_type: ActionType) -> None:
        """Record an execution intent."""
        key = action_type.value
        self.intents_total[key] = self.intents_total.get(key, 0) + 1

    def record_event(self, event: ExecutionEvent) -> None:
        """Record an execution event."""
        key = event.event_type
        self.exec_events_total[key] = self.exec_events_total.get(key, 0) + 1

    def set_orders_open(self, symbol: str, side: OrderSide, count: int) -> None:
        """Set current open orders count."""
        self.orders_open[(symbol, side.value)] = count

    def get_metrics(self) -> dict[str, Any]:
        """Get all metrics as dict."""
        return {
            "grinder_intents_total": dict(self.intents_total),
            "grinder_exec_events_total": dict(self.exec_events_total),
            "grinder_orders_open": {
                f"{sym}:{side}": count
                for (sym, side), count in self.orders_open.items()
            },
        }

    def reset(self) -> None:
        """Reset all metrics."""
        self.intents_total.clear()
        self.exec_events_total.clear()
        self.orders_open.clear()


# Global metrics instance (for convenience in M1)
_metrics = ExecutionMetrics()


def get_metrics() -> ExecutionMetrics:
    """Get global metrics instance."""
    return _metrics


def reset_metrics() -> None:
    """Reset global metrics."""
    _metrics.reset()
