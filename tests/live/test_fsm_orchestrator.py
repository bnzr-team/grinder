"""Table-driven tests for OrchestratorFSM.

Launch-13 PR1: Deterministic transition logic + intent permissions.
SSOT: docs/08_STATE_MACHINE.md (Sec 8.9-8.14).
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from grinder.core import SystemState
from grinder.live.fsm_orchestrator import (
    FsmConfig,
    OrchestratorFSM,
    OrchestratorInputs,
    TransitionEvent,
    TransitionReason,
    allowed_intents,
    is_intent_allowed,
)
from grinder.risk.drawdown_guard_v1 import OrderIntent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = 1_000_000


def _inputs(
    ts_ms: int = _BASE_TS,
    kill_switch: bool = False,
    drawdown: bool = False,
    feed_stale: bool = False,
    tox: str = "LOW",
    position_notional_usd: float | None = 100.0,
    operator: str | None = None,
) -> OrchestratorInputs:
    # Map surrogate bool/str to numeric fields (PR-A2a, PR-A3, PR-A4).
    # Values chosen to be clearly above/below default FsmConfig thresholds.
    # position_notional_usd: 100.0 = above $10 threshold (blocks recovery),
    #   0.0 = below threshold (allows recovery), None = unknown (blocks recovery).
    feed_gap_ms = 10_000 if feed_stale else 0  # 10000 > 5000 threshold
    spread_bps = 80.0 if tox == "MID" else 0.0  # 80 > 50 threshold
    tox_score_bps = 600.0 if tox == "HIGH" else 0.0  # 600 > 500 threshold
    drawdown_pct = 0.25 if drawdown else 0.0  # 0.25 > 0.20 threshold
    return OrchestratorInputs(
        ts_ms=ts_ms,
        kill_switch_active=kill_switch,
        drawdown_pct=drawdown_pct,
        feed_gap_ms=feed_gap_ms,
        spread_bps=spread_bps,
        toxicity_score_bps=tox_score_bps,
        position_notional_usd=position_notional_usd,
        operator_override=operator,
    )


def _fsm(
    state: SystemState = SystemState.INIT,
    enter_ts: int = 0,
    cooldown_ms: int = 30_000,
) -> OrchestratorFSM:
    return OrchestratorFSM(
        state=state,
        state_enter_ts=enter_ts,
        config=FsmConfig(cooldown_ms=cooldown_ms),
    )


# ===========================================================================
# 1. EMERGENCY transitions from various states
# ===========================================================================


class TestEmergencyTransitions:
    """SSOT Sec 8.12 Priority 1-2: kill-switch / dd_breach -> EMERGENCY."""

    @pytest.mark.parametrize(
        "start_state",
        [
            SystemState.READY,
            SystemState.ACTIVE,
            SystemState.THROTTLED,
            SystemState.PAUSED,
            SystemState.DEGRADED,
        ],
    )
    def test_kill_switch_triggers_emergency(self, start_state: SystemState) -> None:
        fsm = _fsm(state=start_state)
        event = fsm.tick(_inputs(kill_switch=True))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.KILL_SWITCH

    @pytest.mark.parametrize(
        "start_state",
        [
            SystemState.READY,
            SystemState.ACTIVE,
            SystemState.THROTTLED,
            SystemState.PAUSED,
            SystemState.DEGRADED,
        ],
    )
    def test_drawdown_breach_triggers_emergency(self, start_state: SystemState) -> None:
        fsm = _fsm(state=start_state)
        event = fsm.tick(_inputs(drawdown=True))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.DD_BREACH

    @pytest.mark.parametrize(
        "start_state",
        [
            SystemState.READY,
            SystemState.ACTIVE,
            SystemState.THROTTLED,
            SystemState.PAUSED,
            SystemState.DEGRADED,
        ],
    )
    def test_operator_emergency_triggers_emergency(self, start_state: SystemState) -> None:
        fsm = _fsm(state=start_state)
        event = fsm.tick(_inputs(operator="EMERGENCY"))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.OPERATOR_EMERGENCY

    def test_emergency_does_not_re_enter_emergency(self) -> None:
        """Already in EMERGENCY: emergency triggers don't cause a transition."""
        fsm = _fsm(state=SystemState.EMERGENCY)
        event = fsm.tick(_inputs(kill_switch=True))
        assert event is None


# ===========================================================================
# 2. DEGRADED transitions
# ===========================================================================


class TestDegradedTransitions:
    """SSOT Sec 8.12 Priority 3: feed_stale -> DEGRADED."""

    @pytest.mark.parametrize(
        "start_state",
        [SystemState.READY, SystemState.ACTIVE, SystemState.THROTTLED, SystemState.PAUSED],
    )
    def test_feed_stale_triggers_degraded(self, start_state: SystemState) -> None:
        fsm = _fsm(state=start_state)
        event = fsm.tick(_inputs(feed_stale=True))
        assert event is not None
        assert event.to_state == SystemState.DEGRADED
        assert event.reason == TransitionReason.FEED_STALE

    def test_degraded_stays_if_feed_still_stale(self) -> None:
        fsm = _fsm(state=SystemState.DEGRADED, enter_ts=0)
        event = fsm.tick(_inputs(ts_ms=100_000, feed_stale=True))
        assert event is None

    def test_degraded_recovers_to_ready_after_cooldown(self) -> None:
        fsm = _fsm(state=SystemState.DEGRADED, enter_ts=0, cooldown_ms=10_000)
        # Before cooldown: no recovery
        event = fsm.tick(_inputs(ts_ms=5_000))
        assert event is None
        # After cooldown: recover to READY
        event = fsm.tick(_inputs(ts_ms=10_000))
        assert event is not None
        assert event.to_state == SystemState.READY
        assert event.reason == TransitionReason.FEED_RECOVERED


# ===========================================================================
# 3. Normal flow: INIT -> READY -> ACTIVE
# ===========================================================================


class TestNormalFlow:
    """Happy path transitions."""

    def test_init_to_ready(self) -> None:
        fsm = _fsm()
        event = fsm.tick(_inputs())
        assert event is not None
        assert event.from_state == SystemState.INIT
        assert event.to_state == SystemState.READY
        assert event.reason == TransitionReason.HEALTH_OK

    def test_init_stays_if_feed_stale(self) -> None:
        fsm = _fsm()
        event = fsm.tick(_inputs(feed_stale=True))
        assert event is None

    def test_init_stays_if_kill_switch(self) -> None:
        fsm = _fsm()
        event = fsm.tick(_inputs(kill_switch=True))
        assert event is None

    def test_ready_to_active(self) -> None:
        fsm = _fsm(state=SystemState.READY)
        event = fsm.tick(_inputs())
        assert event is not None
        assert event.from_state == SystemState.READY
        assert event.to_state == SystemState.ACTIVE
        assert event.reason == TransitionReason.FEEDS_READY

    def test_full_happy_path(self) -> None:
        """INIT -> READY -> ACTIVE in 2 ticks."""
        fsm = _fsm()
        e1 = fsm.tick(_inputs(ts_ms=1000))
        assert e1 is not None
        assert e1.to_state == SystemState.READY

        e2 = fsm.tick(_inputs(ts_ms=2000))
        assert e2 is not None
        assert e2.to_state == SystemState.ACTIVE


# ===========================================================================
# 4. Toxicity transitions
# ===========================================================================


class TestToxicityTransitions:
    """Sec 8.12 Priority 5-6: tox_high -> PAUSED, tox_mid -> THROTTLED."""

    def test_active_to_throttled_on_tox_mid(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(tox="MID"))
        assert event is not None
        assert event.to_state == SystemState.THROTTLED
        assert event.reason == TransitionReason.TOX_MID

    def test_active_to_paused_on_tox_high(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(tox="HIGH"))
        assert event is not None
        assert event.to_state == SystemState.PAUSED
        assert event.reason == TransitionReason.TOX_HIGH

    def test_throttled_to_paused_on_tox_high(self) -> None:
        fsm = _fsm(state=SystemState.THROTTLED)
        event = fsm.tick(_inputs(tox="HIGH"))
        assert event is not None
        assert event.to_state == SystemState.PAUSED
        assert event.reason == TransitionReason.TOX_HIGH

    def test_throttled_stays_on_tox_mid(self) -> None:
        """Already throttled, tox still MID: no transition."""
        fsm = _fsm(state=SystemState.THROTTLED)
        event = fsm.tick(_inputs(tox="MID"))
        assert event is None


# ===========================================================================
# 5. Cooldown / anti-flap
# ===========================================================================


class TestCooldown:
    """Sec 8.12: Recovery blocked until cooldown elapsed."""

    def test_throttled_no_recovery_before_cooldown(self) -> None:
        fsm = _fsm(state=SystemState.THROTTLED, enter_ts=100, cooldown_ms=30_000)
        event = fsm.tick(_inputs(ts_ms=29_999 + 100, tox="LOW"))
        assert event is None

    def test_throttled_recovers_after_cooldown(self) -> None:
        fsm = _fsm(state=SystemState.THROTTLED, enter_ts=100, cooldown_ms=30_000)
        event = fsm.tick(_inputs(ts_ms=30_100, tox="LOW"))
        assert event is not None
        assert event.to_state == SystemState.ACTIVE
        assert event.reason == TransitionReason.TOX_LOW_COOLDOWN

    def test_paused_no_recovery_before_cooldown(self) -> None:
        fsm = _fsm(state=SystemState.PAUSED, enter_ts=0, cooldown_ms=10_000)
        event = fsm.tick(_inputs(ts_ms=9_999, tox="LOW"))
        assert event is None

    def test_paused_recovers_to_active_after_cooldown_tox_low(self) -> None:
        fsm = _fsm(state=SystemState.PAUSED, enter_ts=0, cooldown_ms=10_000)
        event = fsm.tick(_inputs(ts_ms=10_000, tox="LOW"))
        assert event is not None
        assert event.to_state == SystemState.ACTIVE
        assert event.reason == TransitionReason.TOX_LOW_COOLDOWN

    def test_paused_recovers_to_throttled_after_cooldown_tox_mid(self) -> None:
        fsm = _fsm(state=SystemState.PAUSED, enter_ts=0, cooldown_ms=10_000)
        event = fsm.tick(_inputs(ts_ms=10_000, tox="MID"))
        assert event is not None
        assert event.to_state == SystemState.THROTTLED
        assert event.reason == TransitionReason.TOX_MID_COOLDOWN


# ===========================================================================
# 6. EMERGENCY recovery
# ===========================================================================


class TestEmergencyRecovery:
    """Sec 8.10 MUST NOT #4: EMERGENCY -> ACTIVE directly is forbidden."""

    def test_emergency_no_auto_recovery(self) -> None:
        """EMERGENCY stays with large position, even after long time."""
        fsm = _fsm(state=SystemState.EMERGENCY, enter_ts=0)
        event = fsm.tick(_inputs(ts_ms=999_999, tox="LOW"))
        assert event is None

    def test_emergency_to_paused_on_position_reduced(self) -> None:
        fsm = _fsm(state=SystemState.EMERGENCY, enter_ts=0)
        event = fsm.tick(_inputs(position_notional_usd=0.0))
        assert event is not None
        assert event.to_state == SystemState.PAUSED
        assert event.reason == TransitionReason.POSITION_REDUCED

    def test_emergency_stays_if_kill_switch_still_active(self) -> None:
        fsm = _fsm(state=SystemState.EMERGENCY, enter_ts=0)
        event = fsm.tick(_inputs(position_notional_usd=0.0, kill_switch=True))
        assert event is None

    def test_emergency_stays_if_drawdown_still_breached(self) -> None:
        fsm = _fsm(state=SystemState.EMERGENCY, enter_ts=0)
        event = fsm.tick(_inputs(position_notional_usd=0.0, drawdown=True))
        assert event is None

    def test_emergency_never_goes_directly_to_active(self) -> None:
        """Even with everything clear, EMERGENCY -> PAUSED first."""
        fsm = _fsm(state=SystemState.EMERGENCY, enter_ts=0, cooldown_ms=0)
        e1 = fsm.tick(_inputs(ts_ms=1, position_notional_usd=0.0))
        assert e1 is not None
        assert e1.to_state == SystemState.PAUSED
        # Then PAUSED -> ACTIVE after cooldown
        e2 = fsm.tick(_inputs(ts_ms=2))
        assert e2 is not None
        assert e2.to_state == SystemState.ACTIVE


# ===========================================================================
# 7. Priority order
# ===========================================================================


class TestPriorityOrder:
    """Sec 8.12: When multiple triggers fire, highest priority wins."""

    def test_kill_switch_beats_feed_stale(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(kill_switch=True, feed_stale=True))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.KILL_SWITCH

    def test_kill_switch_beats_tox_high(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(kill_switch=True, tox="HIGH"))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.KILL_SWITCH

    def test_drawdown_beats_feed_stale(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(drawdown=True, feed_stale=True))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.DD_BREACH

    def test_operator_emergency_beats_drawdown(self) -> None:
        """OPERATOR_EMERGENCY has same priority tier as KILL_SWITCH (both -> EMERGENCY)."""
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(operator="EMERGENCY", drawdown=True))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        # kill_switch checked first, then operator_emergency, then drawdown
        assert event.reason == TransitionReason.OPERATOR_EMERGENCY

    def test_feed_stale_beats_tox_high(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(feed_stale=True, tox="HIGH"))
        assert event is not None
        # Emergency triggers are checked first (none here), then feed_stale
        assert event.to_state == SystemState.DEGRADED
        assert event.reason == TransitionReason.FEED_STALE

    def test_operator_pause_beats_tox(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(operator="PAUSE", tox="MID"))
        assert event is not None
        assert event.to_state == SystemState.PAUSED
        assert event.reason == TransitionReason.OPERATOR_PAUSE

    def test_tox_high_beats_tox_mid(self) -> None:
        """HIGH and MID can't both be true (tox_level is one value), but HIGH -> PAUSED."""
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(tox="HIGH"))
        assert event is not None
        assert event.to_state == SystemState.PAUSED
        assert event.reason == TransitionReason.TOX_HIGH


# ===========================================================================
# 8. Intent permissions matrix
# ===========================================================================


class TestIntentPermissions:
    """Sec 8.11: Action permissions by state."""

    def test_init_blocks_everything(self) -> None:
        assert allowed_intents(SystemState.INIT) == frozenset()

    def test_ready_allows_cancel_only(self) -> None:
        assert allowed_intents(SystemState.READY) == frozenset({OrderIntent.CANCEL})

    def test_active_allows_all(self) -> None:
        assert allowed_intents(SystemState.ACTIVE) == frozenset(
            {OrderIntent.INCREASE_RISK, OrderIntent.REDUCE_RISK, OrderIntent.CANCEL}
        )

    @pytest.mark.parametrize(
        "state",
        [
            SystemState.THROTTLED,
            SystemState.PAUSED,
            SystemState.DEGRADED,
            SystemState.EMERGENCY,
        ],
    )
    def test_restrictive_states_block_increase_risk(self, state: SystemState) -> None:
        assert not is_intent_allowed(state, OrderIntent.INCREASE_RISK)
        assert is_intent_allowed(state, OrderIntent.REDUCE_RISK)
        assert is_intent_allowed(state, OrderIntent.CANCEL)

    def test_increase_risk_only_in_active(self) -> None:
        for state in SystemState:
            if state == SystemState.ACTIVE:
                assert is_intent_allowed(state, OrderIntent.INCREASE_RISK)
            else:
                assert not is_intent_allowed(state, OrderIntent.INCREASE_RISK)


# ===========================================================================
# 9. TransitionEvent properties
# ===========================================================================


class TestTransitionEvent:
    """TransitionEvent is frozen (immutable) and carries all required fields."""

    def test_event_is_immutable(self) -> None:
        event = TransitionEvent(
            ts_ms=1000,
            from_state=SystemState.ACTIVE,
            to_state=SystemState.EMERGENCY,
            reason=TransitionReason.DD_BREACH,
        )
        with pytest.raises(AttributeError):
            event.ts_ms = 2000  # type: ignore[misc]

    def test_event_carries_all_fields(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.tick(_inputs(ts_ms=5000, kill_switch=True))
        assert event is not None
        assert event.ts_ms == 5000
        assert event.from_state == SystemState.ACTIVE
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.KILL_SWITCH
        assert event.evidence_ref is None


# ===========================================================================
# 10. Force transition
# ===========================================================================


class TestForceTransition:
    """force() always succeeds (operator override)."""

    def test_force_updates_state(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.force(SystemState.PAUSED, TransitionReason.OPERATOR_PAUSE, ts_ms=9999)
        assert event.from_state == SystemState.ACTIVE
        assert event.to_state == SystemState.PAUSED
        assert fsm.state == SystemState.PAUSED
        assert fsm.state_enter_ts == 9999

    def test_force_is_recorded_as_last_transition(self) -> None:
        fsm = _fsm(state=SystemState.ACTIVE)
        event = fsm.force(SystemState.EMERGENCY, TransitionReason.OPERATOR_EMERGENCY, ts_ms=123)
        assert fsm.last_transition is event


# ===========================================================================
# 11. Determinism
# ===========================================================================


class TestDeterminism:
    """Same (state, inputs) -> same output. Required by SSOT Sec 8.10 MUST #3."""

    def test_same_inputs_same_output(self) -> None:
        inp = _inputs(ts_ms=50_000, tox="HIGH")
        for _ in range(10):
            fsm = _fsm(state=SystemState.ACTIVE, enter_ts=0)
            event = fsm.tick(inp)
            assert event is not None
            assert event.to_state == SystemState.PAUSED
            assert event.reason == TransitionReason.TOX_HIGH

    def test_no_transition_is_deterministic(self) -> None:
        inp = _inputs(ts_ms=5000, tox="LOW")
        for _ in range(10):
            fsm = _fsm(state=SystemState.ACTIVE, enter_ts=0)
            event = fsm.tick(inp)
            assert event is None


# ===========================================================================
# 12. Operator override
# ===========================================================================


class TestOperatorOverride:
    """Operator pause prevents recovery from PAUSED."""

    def test_paused_stays_if_operator_pause_active(self) -> None:
        fsm = _fsm(state=SystemState.PAUSED, enter_ts=0, cooldown_ms=0)
        event = fsm.tick(_inputs(ts_ms=999_999, operator="PAUSE", tox="LOW"))
        assert event is None

    def test_ready_to_paused_on_operator_pause(self) -> None:
        fsm = _fsm(state=SystemState.READY)
        event = fsm.tick(_inputs(operator="PAUSE"))
        assert event is not None
        assert event.to_state == SystemState.PAUSED
        assert event.reason == TransitionReason.OPERATOR_PAUSE


# ===========================================================================
# 13. SSOT contract: state graph + disallowed edges
# ===========================================================================


class TestSsotContract:
    """Verify FSM implementation matches SSOT (docs/08_STATE_MACHINE.md).

    Sec 8.2: exactly 7 states.
    Sec 8.10 MUST NOT #2: no state skipping.
    Sec 8.10 MUST NOT #4: EMERGENCY -> ACTIVE directly is forbidden.
    """

    SSOT_STATES: ClassVar[set[str]] = {
        "INIT",
        "READY",
        "ACTIVE",
        "THROTTLED",
        "PAUSED",
        "DEGRADED",
        "EMERGENCY",
    }

    def test_system_state_matches_ssot(self) -> None:
        """SystemState enum has exactly the 7 SSOT states, no more, no less."""
        actual = {s.name for s in SystemState}
        assert actual == self.SSOT_STATES

    # Disallowed direct transitions per SSOT Sec 8.3 + 8.10.
    # Each tuple is (from_state, to_state) that must NEVER happen in one tick.
    DISALLOWED_EDGES: ClassVar[list[tuple[SystemState, SystemState]]] = [
        # MUST NOT #2: no state skipping
        (SystemState.INIT, SystemState.ACTIVE),
        (SystemState.INIT, SystemState.THROTTLED),
        (SystemState.INIT, SystemState.PAUSED),
        (SystemState.INIT, SystemState.DEGRADED),
        (SystemState.INIT, SystemState.EMERGENCY),
        # MUST NOT #4: EMERGENCY -> ACTIVE directly
        (SystemState.EMERGENCY, SystemState.ACTIVE),
        (SystemState.EMERGENCY, SystemState.READY),
        (SystemState.EMERGENCY, SystemState.THROTTLED),
        (SystemState.EMERGENCY, SystemState.DEGRADED),
    ]

    @pytest.mark.parametrize("from_state,to_state", DISALLOWED_EDGES)
    def test_disallowed_edge_never_produced(
        self, from_state: SystemState, to_state: SystemState
    ) -> None:
        """Exhaustively try all input combos; the disallowed edge must never appear."""
        tox_levels = ["LOW", "MID", "HIGH"]
        overrides: list[str | None] = [None, "PAUSE", "EMERGENCY"]
        feed_stale_vals = [False, True]
        for tox in tox_levels:
            for ks in (False, True):
                for dd in (False, True):
                    for fs in feed_stale_vals:
                        for pn in (100.0, 0.0, None):
                            for op in overrides:
                                fsm = _fsm(state=from_state, enter_ts=0, cooldown_ms=0)
                                event = fsm.tick(
                                    _inputs(
                                        ts_ms=_BASE_TS,
                                        kill_switch=ks,
                                        drawdown=dd,
                                        feed_stale=fs,
                                        tox=tox,
                                        position_notional_usd=pn,
                                        operator=op,
                                    )
                                )
                                if event is not None:
                                    assert event.to_state != to_state, (
                                        f"Disallowed edge {from_state.name} -> {to_state.name} "
                                        f"produced with inputs: tox={tox}, ks={ks}, dd={dd}, "
                                        f"fs={fs}, pn={pn}, op={op}"
                                    )
