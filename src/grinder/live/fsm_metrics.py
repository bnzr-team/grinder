"""FSM metrics for Prometheus /metrics endpoint.

Launch-13 PR2: Runtime observability for OrchestratorFSM transitions.
SSOT: docs/08_STATE_MACHINE.md (Sec 8.13).

Design:
- Pure dataclass singleton (same pattern as connectors/metrics.py)
- Thread-safe via dict operations (GIL-protected)
- No external dependencies
- One-hot current_state gauge derived from SystemState enum
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.core import SystemState

if TYPE_CHECKING:
    from grinder.live.fsm_orchestrator import TransitionEvent
    from grinder.risk.drawdown_guard_v1 import OrderIntent

# Metric names (stable contract)
METRIC_FSM_CURRENT_STATE = "grinder_fsm_current_state"
METRIC_FSM_STATE_DURATION = "grinder_fsm_state_duration_seconds"
METRIC_FSM_TRANSITIONS = "grinder_fsm_transitions_total"
METRIC_FSM_ACTION_BLOCKED = "grinder_fsm_action_blocked_total"


@dataclass
class FsmMetrics:
    """Metrics collector for FSM state machine.

    Thread-safe via simple dict operations (GIL protection).
    Production-ready for Prometheus export.

    Attributes:
        transitions: {(from_state, to_state, reason): count}
        _current_state: Last set SystemState (for one-hot encoding)
        state_duration_s: Time in current state (seconds)
        action_blocked: {(state, intent): count}
    """

    transitions: dict[tuple[str, str, str], int] = field(default_factory=dict)
    _current_state: SystemState | None = None
    state_duration_s: float = 0.0
    action_blocked: dict[tuple[str, str], int] = field(default_factory=dict)

    def record_transition(self, event: TransitionEvent) -> None:
        """Record a state transition.

        Args:
            event: Immutable TransitionEvent from FSM tick.
        """
        key = (event.from_state.name, event.to_state.name, event.reason.value)
        self.transitions[key] = self.transitions.get(key, 0) + 1

    def set_current_state(self, state: SystemState) -> None:
        """Set current FSM state for one-hot gauge."""
        self._current_state = state

    def set_state_duration(self, seconds: float) -> None:
        """Set time in current state (seconds)."""
        self.state_duration_s = seconds

    def record_action_blocked(self, state: SystemState, intent: OrderIntent) -> None:
        """Record an action blocked by FSM permission matrix.

        Args:
            state: Current SystemState when block occurred.
            intent: OrderIntent that was blocked.
        """
        key = (state.name, intent.value)
        self.action_blocked[key] = self.action_blocked.get(key, 0) + 1

    def to_prometheus_lines(self) -> list[str]:
        """Generate Prometheus text format lines.

        Returns:
            List of lines in Prometheus exposition format.
        """
        lines: list[str] = []

        # FSM current state (one-hot gauge: all 7 states every build)
        lines.extend(
            [
                f"# HELP {METRIC_FSM_CURRENT_STATE} Current FSM state (1=current, 0=other)",
                f"# TYPE {METRIC_FSM_CURRENT_STATE} gauge",
            ]
        )
        for state in SystemState:
            value = 1 if state == self._current_state else 0
            lines.append(f'{METRIC_FSM_CURRENT_STATE}{{state="{state.name}"}} {value}')

        # FSM state duration (gauge)
        lines.extend(
            [
                f"# HELP {METRIC_FSM_STATE_DURATION} Time in current state (seconds)",
                f"# TYPE {METRIC_FSM_STATE_DURATION} gauge",
                f"{METRIC_FSM_STATE_DURATION} {self.state_duration_s:.2f}",
            ]
        )

        # FSM transitions (counter)
        lines.extend(
            [
                f"# HELP {METRIC_FSM_TRANSITIONS} Total state transitions by from/to/reason",
                f"# TYPE {METRIC_FSM_TRANSITIONS} counter",
            ]
        )
        if self.transitions:
            for (from_s, to_s, reason), count in sorted(self.transitions.items()):
                lines.append(
                    f'{METRIC_FSM_TRANSITIONS}{{from_state="{from_s}",'
                    f'to_state="{to_s}",reason="{reason}"}} {count}'
                )
        else:
            lines.append(
                f'{METRIC_FSM_TRANSITIONS}{{from_state="none",to_state="none",reason="none"}} 0'
            )

        # FSM action blocked (counter)
        lines.extend(
            [
                f"# HELP {METRIC_FSM_ACTION_BLOCKED} Actions blocked by FSM permission matrix",
                f"# TYPE {METRIC_FSM_ACTION_BLOCKED} counter",
            ]
        )
        if self.action_blocked:
            for (state_name, intent_val), count in sorted(self.action_blocked.items()):
                lines.append(
                    f'{METRIC_FSM_ACTION_BLOCKED}{{state="{state_name}",'
                    f'intent="{intent_val}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_FSM_ACTION_BLOCKED}{{state="none",intent="none"}} 0')

        return lines


# Global singleton
_metrics: FsmMetrics | None = None


def get_fsm_metrics() -> FsmMetrics:
    """Get or create global FSM metrics instance."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = FsmMetrics()
    return _metrics


def reset_fsm_metrics() -> None:
    """Reset FSM metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
