"""Pure deterministic FSM orchestrator for system lifecycle.

Launch-13 PR1: Core types + transition logic.
SSOT: docs/08_STATE_MACHINE.md (Sec 8.9-8.14).

Design constraints (from SSOT):
- Pure logic: no I/O, no logging, no metrics emission.
- Deterministic: same (state, inputs) -> same (next_state, reason).
- Inputs passed as frozen snapshots (OrchestratorInputs).
- Caller is responsible for logging/metrics/evidence on transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from grinder.core import SystemState
from grinder.risk.drawdown_guard_v1 import OrderIntent


class TransitionReason(Enum):
    """Why the FSM changed state. Every transition carries exactly one reason."""

    # INIT -> READY
    HEALTH_OK = "HEALTH_OK"

    # READY -> ACTIVE
    FEEDS_READY = "FEEDS_READY"

    # -> THROTTLED
    TOX_MID = "TOX_MID"

    # -> PAUSED
    TOX_HIGH = "TOX_HIGH"
    OPERATOR_PAUSE = "OPERATOR_PAUSE"

    # -> ACTIVE (recovery)
    TOX_LOW_COOLDOWN = "TOX_LOW_COOLDOWN"
    TOX_MID_COOLDOWN = "TOX_MID_COOLDOWN"
    FEED_RECOVERED = "FEED_RECOVERED"

    # -> DEGRADED
    FEED_STALE = "FEED_STALE"

    # -> EMERGENCY
    DD_BREACH = "DD_BREACH"
    KILL_SWITCH = "KILL_SWITCH"
    OPERATOR_EMERGENCY = "OPERATOR_EMERGENCY"

    # EMERGENCY -> PAUSED
    POSITION_REDUCED = "POSITION_REDUCED"


@dataclass(frozen=True)
class TransitionEvent:
    """Immutable record emitted on every transition."""

    ts_ms: int
    from_state: SystemState
    to_state: SystemState
    reason: TransitionReason
    evidence_ref: str | None = None


@dataclass(frozen=True)
class OrchestratorInputs:
    """Snapshot of inputs the FSM evaluates on each tick.

    All fields are value-level snapshots; no mutable references.
    """

    ts_ms: int
    kill_switch_active: bool
    drawdown_breached: bool
    feed_stale: bool
    toxicity_level: str  # "LOW" | "MID" | "HIGH"
    position_reduced: bool  # position below emergency exit threshold
    operator_override: str | None  # "PAUSE" | "EMERGENCY" | None


@dataclass(frozen=True)
class FsmConfig:
    """Configuration for FSM behavior."""

    cooldown_ms: int = 30_000  # min time in state before recovery transition


# ---------------------------------------------------------------------------
# Action permissions matrix (Sec 8.11)
# ---------------------------------------------------------------------------

_PERMISSIONS: dict[SystemState, set[OrderIntent]] = {
    SystemState.INIT: set(),
    SystemState.READY: {OrderIntent.CANCEL},
    SystemState.ACTIVE: {OrderIntent.INCREASE_RISK, OrderIntent.REDUCE_RISK, OrderIntent.CANCEL},
    SystemState.THROTTLED: {OrderIntent.REDUCE_RISK, OrderIntent.CANCEL},
    SystemState.PAUSED: {OrderIntent.REDUCE_RISK, OrderIntent.CANCEL},
    SystemState.DEGRADED: {OrderIntent.REDUCE_RISK, OrderIntent.CANCEL},
    SystemState.EMERGENCY: {OrderIntent.REDUCE_RISK, OrderIntent.CANCEL},
}


def allowed_intents(state: SystemState) -> frozenset[OrderIntent]:
    """Return the set of OrderIntents allowed in the given state."""
    return frozenset(_PERMISSIONS.get(state, set()))


def is_intent_allowed(state: SystemState, intent: OrderIntent) -> bool:
    """Check if a specific intent is allowed in the given state."""
    return intent in _PERMISSIONS.get(state, set())


# ---------------------------------------------------------------------------
# FSM orchestrator (pure logic)
# ---------------------------------------------------------------------------


class OrchestratorFSM:
    """Centralized lifecycle state machine.

    MUST:
    - Return TransitionEvent on every state change.
    - Be deterministic: same (state, inputs) -> same (next_state, reason).
    - Never skip states (e.g., INIT -> ACTIVE is illegal).

    MUST NOT:
    - Perform I/O (logging, metrics, network).
    - Hold mutable references to external state.
    """

    def __init__(
        self,
        state: SystemState = SystemState.INIT,
        state_enter_ts: int = 0,
        config: FsmConfig | None = None,
    ) -> None:
        self.state = state
        self.state_enter_ts = state_enter_ts
        self.last_transition: TransitionEvent | None = None
        self._config = config or FsmConfig()

    def time_in_state_ms(self, now_ts: int) -> int:
        """Milliseconds in current state."""
        return now_ts - self.state_enter_ts

    def _cooldown_elapsed(self, now_ts: int) -> bool:
        return self.time_in_state_ms(now_ts) >= self._config.cooldown_ms

    def tick(self, inputs: OrchestratorInputs) -> TransitionEvent | None:
        """Evaluate inputs. Return TransitionEvent if state changed, else None.

        Trigger priority (highest first, per Sec 8.12):
        1. KILL_SWITCH / OPERATOR_EMERGENCY -> EMERGENCY
        2. DD_BREACH -> EMERGENCY
        3. FEED_STALE -> DEGRADED
        4. OPERATOR_PAUSE -> PAUSED
        5. TOX_HIGH -> PAUSED
        6. TOX_MID -> THROTTLED
        7. Recovery triggers (lowest)
        """
        result = self._evaluate(inputs)
        if result is None:
            return None

        new_state, reason = result
        event = TransitionEvent(
            ts_ms=inputs.ts_ms,
            from_state=self.state,
            to_state=new_state,
            reason=reason,
        )
        self.state = new_state
        self.state_enter_ts = inputs.ts_ms
        self.last_transition = event
        return event

    def force(
        self,
        to_state: SystemState,
        reason: TransitionReason,
        ts_ms: int,
    ) -> TransitionEvent:
        """Operator-forced transition. Always succeeds."""
        event = TransitionEvent(
            ts_ms=ts_ms,
            from_state=self.state,
            to_state=to_state,
            reason=reason,
        )
        self.state = to_state
        self.state_enter_ts = ts_ms
        self.last_transition = event
        return event

    # ------------------------------------------------------------------
    # Private: evaluation logic (priority-ordered)
    # ------------------------------------------------------------------

    _STATE_HANDLERS: ClassVar[dict[SystemState, str]] = {
        SystemState.INIT: "_eval_init",
        SystemState.READY: "_eval_ready",
        SystemState.ACTIVE: "_eval_active",
        SystemState.THROTTLED: "_eval_throttled",
        SystemState.PAUSED: "_eval_paused",
        SystemState.DEGRADED: "_eval_degraded",
        SystemState.EMERGENCY: "_eval_emergency",
    }

    def _evaluate(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """Core transition logic. Returns (new_state, reason) or None."""
        # Priority 1: EMERGENCY triggers (from any non-INIT state)
        if self.state != SystemState.INIT:
            emergency = self._check_emergency(inp)
            if emergency is not None:
                return emergency

        # Dispatch to state-specific handler
        handler_name = self._STATE_HANDLERS.get(self.state)
        if handler_name is None:
            return None  # pragma: no cover
        handler = getattr(self, handler_name)
        return handler(inp)  # type: ignore[no-any-return]

    def _check_emergency(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """Check emergency triggers. Applies from any state except INIT and EMERGENCY."""
        if self.state == SystemState.EMERGENCY:
            return None  # already in EMERGENCY

        if inp.kill_switch_active:
            return (SystemState.EMERGENCY, TransitionReason.KILL_SWITCH)
        if inp.operator_override == "EMERGENCY":
            return (SystemState.EMERGENCY, TransitionReason.OPERATOR_EMERGENCY)
        if inp.drawdown_breached:
            return (SystemState.EMERGENCY, TransitionReason.DD_BREACH)
        return None

    def _eval_init(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """INIT -> READY when health checks pass (no emergency triggers in INIT)."""
        if not inp.kill_switch_active and not inp.feed_stale:
            return (SystemState.READY, TransitionReason.HEALTH_OK)
        return None

    def _eval_ready(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """READY -> ACTIVE when feeds ready, or -> DEGRADED on feed stale."""
        # Priority 3: feed stale -> DEGRADED
        if inp.feed_stale:
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        # Recovery: ready -> active when feeds are ok and no operator override
        if inp.operator_override == "PAUSE":
            return (SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE)
        if inp.toxicity_level not in ("MID", "HIGH"):
            return (SystemState.ACTIVE, TransitionReason.FEEDS_READY)
        return None

    def _eval_active(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """ACTIVE -> DEGRADED/PAUSED/THROTTLED based on priority."""
        # Priority 3: feed stale -> DEGRADED
        if inp.feed_stale:
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        # Priority 4: operator pause
        if inp.operator_override == "PAUSE":
            return (SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE)
        # Priority 5: tox high -> PAUSED
        if inp.toxicity_level == "HIGH":
            return (SystemState.PAUSED, TransitionReason.TOX_HIGH)
        # Priority 6: tox mid -> THROTTLED
        if inp.toxicity_level == "MID":
            return (SystemState.THROTTLED, TransitionReason.TOX_MID)
        return None

    def _eval_throttled(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """THROTTLED -> DEGRADED/PAUSED (escalation) or -> ACTIVE (recovery with cooldown)."""
        if inp.feed_stale:
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        if inp.operator_override == "PAUSE":
            return (SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE)
        if inp.toxicity_level == "HIGH":
            return (SystemState.PAUSED, TransitionReason.TOX_HIGH)
        # Recovery: only with cooldown and tox LOW
        if inp.toxicity_level == "LOW" and self._cooldown_elapsed(inp.ts_ms):
            return (SystemState.ACTIVE, TransitionReason.TOX_LOW_COOLDOWN)
        return None

    def _eval_paused(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """PAUSED -> DEGRADED (escalation) or -> THROTTLED/ACTIVE (recovery with cooldown)."""
        if inp.feed_stale:
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        # Recovery only after cooldown, no operator override active
        if inp.operator_override == "PAUSE":
            return None  # stay paused
        if not self._cooldown_elapsed(inp.ts_ms):
            return None
        if inp.toxicity_level == "LOW":
            return (SystemState.ACTIVE, TransitionReason.TOX_LOW_COOLDOWN)
        if inp.toxicity_level == "MID":
            return (SystemState.THROTTLED, TransitionReason.TOX_MID_COOLDOWN)
        return None

    def _eval_degraded(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """DEGRADED -> READY (recovery) when feeds recover + cooldown."""
        if inp.feed_stale:
            return None  # stay degraded
        if self._cooldown_elapsed(inp.ts_ms):
            return (SystemState.READY, TransitionReason.FEED_RECOVERED)
        return None

    def _eval_emergency(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """EMERGENCY -> PAUSED only when position reduced. No auto-recovery."""
        if inp.position_reduced and not inp.kill_switch_active and not inp.drawdown_breached:
            return (SystemState.PAUSED, TransitionReason.POSITION_REDUCED)
        return None
