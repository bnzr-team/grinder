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
    Numeric fields carry units in names (*_ms, *_bps) — FSM owns thresholds.
    """

    ts_ms: int
    kill_switch_active: bool
    drawdown_pct: float  # portfolio drawdown as fraction (0.20 = 20%)
    feed_gap_ms: int  # ms since last snapshot for this symbol (0 = first tick)
    spread_bps: float  # current bid-ask spread in basis points
    toxicity_score_bps: float  # price impact in bps (0.0 = no impact)
    position_notional_usd: float | None  # Σ(|qty| * mark_price) USDT; None = unknown
    operator_override: str | None  # "PAUSE" | "EMERGENCY" | None


@dataclass(frozen=True)
class FsmConfig:
    """Configuration for FSM behavior.

    Threshold defaults match prior hardcoded values (meta-test proves this):
    - feed_stale_threshold_ms: was engine GRINDER_FEED_STALE_MS default (5000)
    - spread_spike_threshold_bps: was ToxicityGate.max_spread_bps (50.0)
    - toxicity_high_threshold_bps: was ToxicityGate.max_price_impact_bps (500.0)
    """

    cooldown_ms: int = 30_000  # min time in state before recovery transition
    feed_stale_threshold_ms: int = 5_000  # feed gap above this → stale
    spread_spike_threshold_bps: float = 50.0  # spread above this → MID toxicity
    toxicity_high_threshold_bps: float = 500.0  # price impact above this → HIGH toxicity
    drawdown_threshold_pct: float = (
        0.20  # DD fraction above this → EMERGENCY (was DrawdownGuardV1Config default)
    )
    position_notional_threshold_usd: float = (
        10.0  # recovery threshold: below this USDT = "effectively flat" (not exchange min_notional)
    )


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
        if self._is_dd_breached(inp):
            return (SystemState.EMERGENCY, TransitionReason.DD_BREACH)
        return None

    # ------------------------------------------------------------------
    # Threshold helpers (numeric inputs → bool decisions)
    # ------------------------------------------------------------------

    def _is_feed_stale(self, inp: OrchestratorInputs) -> bool:
        """Feed is stale if gap > 0 (not first tick) and exceeds threshold."""
        return inp.feed_gap_ms > 0 and inp.feed_gap_ms > self._config.feed_stale_threshold_ms

    def _is_toxic_high(self, inp: OrchestratorInputs) -> bool:
        return inp.toxicity_score_bps > self._config.toxicity_high_threshold_bps

    def _is_toxic_mid(self, inp: OrchestratorInputs) -> bool:
        return inp.spread_bps > self._config.spread_spike_threshold_bps

    def _is_toxic_low(self, inp: OrchestratorInputs) -> bool:
        return not self._is_toxic_mid(inp) and not self._is_toxic_high(inp)

    def _is_dd_breached(self, inp: OrchestratorInputs) -> bool:
        """Drawdown breached if pct >= threshold (was bool drawdown_breached)."""
        return inp.drawdown_pct >= self._config.drawdown_threshold_pct

    def _is_position_large(self, inp: OrchestratorInputs) -> bool:
        """Position large (or unknown) blocks EMERGENCY recovery.

        None = unknown (no AccountSyncer data yet) → conservatively block.
        >= threshold = position still significant → block recovery.
        """
        if inp.position_notional_usd is None:
            return True
        return inp.position_notional_usd >= self._config.position_notional_threshold_usd

    # ------------------------------------------------------------------
    # State-specific evaluation (priority-ordered)
    # ------------------------------------------------------------------

    def _eval_init(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """INIT -> READY when health checks pass (no emergency triggers in INIT)."""
        if not inp.kill_switch_active and not self._is_feed_stale(inp):
            return (SystemState.READY, TransitionReason.HEALTH_OK)
        return None

    def _eval_ready(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """READY -> ACTIVE when feeds ready, or -> DEGRADED on feed stale."""
        # Priority 3: feed stale -> DEGRADED
        if self._is_feed_stale(inp):
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        # Recovery: ready -> active when feeds are ok and no operator override
        if inp.operator_override == "PAUSE":
            return (SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE)
        if self._is_toxic_low(inp):
            return (SystemState.ACTIVE, TransitionReason.FEEDS_READY)
        return None

    def _eval_active(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """ACTIVE -> DEGRADED/PAUSED/THROTTLED based on priority."""
        # Priority 3: feed stale -> DEGRADED
        if self._is_feed_stale(inp):
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        # Priority 4: operator pause
        if inp.operator_override == "PAUSE":
            return (SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE)
        # Priority 5: tox high -> PAUSED
        if self._is_toxic_high(inp):
            return (SystemState.PAUSED, TransitionReason.TOX_HIGH)
        # Priority 6: tox mid -> THROTTLED
        if self._is_toxic_mid(inp):
            return (SystemState.THROTTLED, TransitionReason.TOX_MID)
        return None

    def _eval_throttled(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """THROTTLED -> DEGRADED/PAUSED (escalation) or -> ACTIVE (recovery with cooldown)."""
        if self._is_feed_stale(inp):
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        if inp.operator_override == "PAUSE":
            return (SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE)
        if self._is_toxic_high(inp):
            return (SystemState.PAUSED, TransitionReason.TOX_HIGH)
        # Recovery: only with cooldown and tox LOW
        if self._is_toxic_low(inp) and self._cooldown_elapsed(inp.ts_ms):
            return (SystemState.ACTIVE, TransitionReason.TOX_LOW_COOLDOWN)
        return None

    def _eval_paused(self, inp: OrchestratorInputs) -> tuple[SystemState, TransitionReason] | None:
        """PAUSED -> DEGRADED (escalation) or -> THROTTLED/ACTIVE (recovery with cooldown)."""
        if self._is_feed_stale(inp):
            return (SystemState.DEGRADED, TransitionReason.FEED_STALE)
        # Recovery only after cooldown, no operator override active
        if inp.operator_override == "PAUSE":
            return None  # stay paused
        if not self._cooldown_elapsed(inp.ts_ms):
            return None
        if self._is_toxic_low(inp):
            return (SystemState.ACTIVE, TransitionReason.TOX_LOW_COOLDOWN)
        if self._is_toxic_mid(inp):
            return (SystemState.THROTTLED, TransitionReason.TOX_MID_COOLDOWN)
        return None

    def _eval_degraded(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """DEGRADED -> READY (recovery) when feeds recover + cooldown."""
        if self._is_feed_stale(inp):
            return None  # stay degraded
        if self._cooldown_elapsed(inp.ts_ms):
            return (SystemState.READY, TransitionReason.FEED_RECOVERED)
        return None

    def _eval_emergency(
        self, inp: OrchestratorInputs
    ) -> tuple[SystemState, TransitionReason] | None:
        """EMERGENCY -> PAUSED only when position notional below threshold.

        Requires confirmed measurement (not None) AND below threshold AND
        no active emergency triggers (kill_switch, drawdown).
        """
        if (
            not self._is_position_large(inp)
            and not inp.kill_switch_active
            and not self._is_dd_breached(inp)
        ):
            return (SystemState.PAUSED, TransitionReason.POSITION_REDUCED)
        return None
