"""FSM driver: runtime caller for OrchestratorFSM.

Launch-13 PR2: Thin glue layer that builds inputs, ticks the FSM, and emits
side effects (metrics + structured logs). The FSM itself remains pure.

SSOT: docs/08_STATE_MACHINE.md (Sec 8.13, 8.14).

Design:
- build_inputs() validates string fields, raises ValueError on unknowns.
- FsmDriver owns the clock for state duration (monotonic guard).
- Side effects (logging, metrics) live here, NOT in the FSM.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from grinder.live.fsm_evidence import maybe_emit_transition_evidence
from grinder.live.fsm_metrics import get_fsm_metrics
from grinder.live.fsm_orchestrator import (
    OrchestratorFSM,
    OrchestratorInputs,
    TransitionEvent,
    is_intent_allowed,
)

if TYPE_CHECKING:
    from grinder.core import SystemState
    from grinder.risk.drawdown_guard_v1 import OrderIntent

logger = logging.getLogger(__name__)

VALID_TOX_LEVELS = frozenset({"LOW", "MID", "HIGH"})
VALID_OVERRIDES = frozenset({"PAUSE", "EMERGENCY"})


def build_inputs(
    *,
    ts_ms: int,
    kill_switch_active: bool,
    drawdown_breached: bool,
    feed_stale: bool,
    toxicity_level: str,
    position_reduced: bool,
    operator_override: str | None,
) -> OrchestratorInputs:
    """Build frozen OrchestratorInputs snapshot with validation.

    Raises:
        ValueError: If toxicity_level or operator_override has an invalid value.
    """
    if toxicity_level not in VALID_TOX_LEVELS:
        raise ValueError(
            f"Invalid toxicity_level={toxicity_level!r}, expected one of {sorted(VALID_TOX_LEVELS)}"
        )
    if operator_override is not None and operator_override not in VALID_OVERRIDES:
        raise ValueError(
            f"Invalid operator_override={operator_override!r}, "
            f"expected one of {sorted(VALID_OVERRIDES)} or None"
        )
    return OrchestratorInputs(
        ts_ms=ts_ms,
        kill_switch_active=kill_switch_active,
        drawdown_breached=drawdown_breached,
        feed_stale=feed_stale,
        toxicity_level=toxicity_level,
        position_reduced=position_reduced,
        operator_override=operator_override,
    )


class FsmDriver:
    """Runtime caller for OrchestratorFSM.

    Owns the FSM instance and emits side effects on each step():
    1. Builds OrchestratorInputs from caller-provided signals
    2. Calls fsm.tick()
    3. If transition: emit metrics + structured log
    4. Always: update state gauge + duration gauge
    """

    def __init__(self, fsm: OrchestratorFSM) -> None:
        self._fsm = fsm
        # Safe init: use state_enter_ts if available, else defer to first step()
        self._last_state_change_ms: int | None = getattr(fsm, "state_enter_ts", None)

    @property
    def state(self) -> SystemState:
        """Current FSM state."""
        return self._fsm.state

    def step(
        self,
        *,
        ts_ms: int,
        kill_switch_active: bool,
        drawdown_breached: bool,
        feed_stale: bool,
        toxicity_level: str,
        position_reduced: bool,
        operator_override: str | None,
    ) -> TransitionEvent | None:
        """Single tick: build inputs -> tick FSM -> emit side effects.

        Returns:
            TransitionEvent if state changed, else None.
        """
        inputs = build_inputs(
            ts_ms=ts_ms,
            kill_switch_active=kill_switch_active,
            drawdown_breached=drawdown_breached,
            feed_stale=feed_stale,
            toxicity_level=toxicity_level,
            position_reduced=position_reduced,
            operator_override=operator_override,
        )

        event = self._fsm.tick(inputs)

        # Initialize clock on first step if not set from FSM
        if self._last_state_change_ms is None:
            self._last_state_change_ms = ts_ms

        if event is not None:
            # Emit structured log
            prev_duration_s = max(0, ts_ms - self._last_state_change_ms) / 1000.0
            logger.info(
                "FSM_TRANSITION",
                extra={
                    "from_state": event.from_state.name,
                    "to_state": event.to_state.name,
                    "reason": event.reason.value,
                    "ts_ms": event.ts_ms,
                    "time_in_prev_state_s": prev_duration_s,
                },
            )
            # Emit transition metric
            get_fsm_metrics().record_transition(event)
            # Emit evidence artifact (safe: no-op if env disabled, warns on IO error)
            maybe_emit_transition_evidence(event, inputs)
            # Update clock
            self._last_state_change_ms = ts_ms

        # Always: update state gauge + duration gauge
        metrics = get_fsm_metrics()
        metrics.set_current_state(self._fsm.state)

        # Monotonic guard: clamp to 0 if ts_ms goes backward
        duration_s = max(0, ts_ms - self._last_state_change_ms) / 1000.0
        metrics.set_state_duration(duration_s)

        return event

    def check_intent(self, intent: OrderIntent) -> bool:
        """Check if intent is allowed in current FSM state.

        Emits blocked metric + warning log if denied.

        Returns:
            True if allowed, False if blocked.
        """
        if is_intent_allowed(self._fsm.state, intent):
            return True

        logger.warning(
            "FSM_ACTION_BLOCKED",
            extra={
                "state": self._fsm.state.name,
                "intent": intent.value,
            },
        )
        get_fsm_metrics().record_action_blocked(self._fsm.state, intent)
        return False
