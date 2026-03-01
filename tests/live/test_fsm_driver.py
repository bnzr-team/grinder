"""Tests for FsmDriver (Launch-13 PR2, updated PR-A2a).

Validates:
- build_inputs() validation (valid + invalid numeric/operator values)
- FsmDriver.step() ticks FSM and emits metrics on transition
- FsmDriver.step() updates state gauge + duration gauge every tick
- Monotonic time guard (backward ts_ms clamps duration to 0)
- FsmDriver.check_intent() delegates to is_intent_allowed + emits blocked metric
- Side effects are outside FSM (metrics updated, FSM itself has no I/O)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grinder.core import SystemState
from grinder.live.fsm_driver import VALID_OVERRIDES, FsmDriver, build_inputs
from grinder.live.fsm_evidence import ENV_ARTIFACT_DIR, ENV_ENABLE
from grinder.live.fsm_metrics import get_fsm_metrics, reset_fsm_metrics
from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorFSM, TransitionReason
from grinder.risk.drawdown_guard_v1 import OrderIntent

_BASE_TS = 1_000_000


def _make_driver(
    state: SystemState = SystemState.INIT,
    enter_ts: int = 0,
    cooldown_ms: int = 30_000,
) -> FsmDriver:
    """Create FsmDriver with configured FSM."""
    fsm = OrchestratorFSM(
        state=state,
        state_enter_ts=enter_ts,
        config=FsmConfig(cooldown_ms=cooldown_ms),
    )
    return FsmDriver(fsm)


# ===========================================================================
# 1. build_inputs() validation
# ===========================================================================


class TestBuildInputsValid:
    """build_inputs() with valid values returns correct OrchestratorInputs."""

    def test_valid_inputs(self) -> None:
        inp = build_inputs(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert inp.ts_ms == _BASE_TS
        assert inp.feed_gap_ms == 0
        assert inp.spread_bps == 0.0
        assert inp.toxicity_score_bps == 0.0
        assert inp.operator_override is None

    def test_numeric_fields_accept_positive_values(self) -> None:
        inp = build_inputs(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=5000,
            spread_bps=80.0,
            toxicity_score_bps=200.0,
            position_reduced=False,
            operator_override=None,
        )
        assert inp.feed_gap_ms == 5000
        assert inp.spread_bps == 80.0
        assert inp.toxicity_score_bps == 200.0

    def test_all_overrides_accepted(self) -> None:
        for override in [None, *VALID_OVERRIDES]:
            inp = build_inputs(
                ts_ms=_BASE_TS,
                kill_switch_active=False,
                drawdown_breached=False,
                feed_gap_ms=0,
                spread_bps=0.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override=override,
            )
            assert inp.operator_override == override


class TestBuildInputsInvalid:
    """build_inputs() raises ValueError on invalid values."""

    def test_negative_feed_gap_ms(self) -> None:
        with pytest.raises(ValueError, match="feed_gap_ms must be >= 0"):
            build_inputs(
                ts_ms=_BASE_TS,
                kill_switch_active=False,
                drawdown_breached=False,
                feed_gap_ms=-1,
                spread_bps=0.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_negative_spread_bps(self) -> None:
        with pytest.raises(ValueError, match="spread_bps must be >= 0"):
            build_inputs(
                ts_ms=_BASE_TS,
                kill_switch_active=False,
                drawdown_breached=False,
                feed_gap_ms=0,
                spread_bps=-1.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_negative_toxicity_score_bps(self) -> None:
        with pytest.raises(ValueError, match="toxicity_score_bps must be >= 0"):
            build_inputs(
                ts_ms=_BASE_TS,
                kill_switch_active=False,
                drawdown_breached=False,
                feed_gap_ms=0,
                spread_bps=0.0,
                toxicity_score_bps=-1.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_invalid_operator_override(self) -> None:
        with pytest.raises(ValueError, match="Invalid operator_override"):
            build_inputs(
                ts_ms=_BASE_TS,
                kill_switch_active=False,
                drawdown_breached=False,
                feed_gap_ms=0,
                spread_bps=0.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override="SHUTDOWN",
            )


# ===========================================================================
# 2. FsmDriver.step() — transitions and metrics
# ===========================================================================


class TestStepTransition:
    """FsmDriver.step() ticks FSM and emits metrics on transition."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_step_returns_event_on_transition(self) -> None:
        driver = _make_driver(state=SystemState.INIT)
        event = driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert event is not None
        assert event.from_state == SystemState.INIT
        assert event.to_state == SystemState.READY
        assert event.reason == TransitionReason.HEALTH_OK

    def test_step_emits_transition_metric(self) -> None:
        driver = _make_driver(state=SystemState.INIT)
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        metrics = get_fsm_metrics()
        assert metrics.transitions[("INIT", "READY", "HEALTH_OK")] == 1

    def test_step_returns_none_when_no_transition(self) -> None:
        driver = _make_driver(state=SystemState.ACTIVE)
        event = driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert event is None


class TestStepStateGauge:
    """FsmDriver.step() updates state gauge every tick."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_state_gauge_updated_every_tick(self) -> None:
        driver = _make_driver(state=SystemState.ACTIVE)
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        metrics = get_fsm_metrics()
        assert metrics._current_state == SystemState.ACTIVE

    def test_state_gauge_updates_after_transition(self) -> None:
        driver = _make_driver(state=SystemState.INIT)
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        metrics = get_fsm_metrics()
        assert metrics._current_state == SystemState.READY


# ===========================================================================
# 3. Duration gauge + monotonic time guard
# ===========================================================================


class TestDurationGauge:
    """State duration with driver-owned clock."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_duration_increases_over_ticks(self) -> None:
        driver = _make_driver(state=SystemState.ACTIVE, enter_ts=0)
        driver.step(
            ts_ms=10_000,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        # Duration = (10_000 - 0) / 1000 = 10.0s
        assert get_fsm_metrics().state_duration_s == pytest.approx(10.0)

    def test_duration_resets_on_transition(self) -> None:
        driver = _make_driver(state=SystemState.INIT, enter_ts=0)
        # Transition INIT -> READY at ts=5000
        driver.step(
            ts_ms=5_000,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        # Duration = 0 (just transitioned at ts=5000)
        assert get_fsm_metrics().state_duration_s == pytest.approx(0.0)

    def test_monotonic_guard_clamps_to_zero(self) -> None:
        """If ts_ms goes backward, duration is clamped to 0."""
        driver = _make_driver(state=SystemState.ACTIVE, enter_ts=10_000)
        # ts_ms < enter_ts: backward clock
        driver.step(
            ts_ms=5_000,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert get_fsm_metrics().state_duration_s == pytest.approx(0.0)

    def test_first_step_initializes_clock(self) -> None:
        """If FSM has no state_enter_ts, first step sets the clock."""
        fsm = OrchestratorFSM(state=SystemState.ACTIVE)
        # Manually remove state_enter_ts to simulate missing attr
        driver = FsmDriver(fsm)
        # Override to simulate missing attr scenario
        driver._last_state_change_ms = None

        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        # Duration should be 0 (just initialized)
        assert get_fsm_metrics().state_duration_s == pytest.approx(0.0)


# ===========================================================================
# 4. check_intent()
# ===========================================================================


class TestCheckIntent:
    """FsmDriver.check_intent() delegates to is_intent_allowed."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_allowed_in_active(self) -> None:
        driver = _make_driver(state=SystemState.ACTIVE)
        assert driver.check_intent(OrderIntent.INCREASE_RISK) is True
        assert driver.check_intent(OrderIntent.REDUCE_RISK) is True
        assert driver.check_intent(OrderIntent.CANCEL) is True

    def test_blocked_increase_risk_in_paused(self) -> None:
        driver = _make_driver(state=SystemState.PAUSED)
        assert driver.check_intent(OrderIntent.INCREASE_RISK) is False

    def test_allowed_reduce_risk_in_paused(self) -> None:
        driver = _make_driver(state=SystemState.PAUSED)
        assert driver.check_intent(OrderIntent.REDUCE_RISK) is True

    def test_blocked_emits_metric(self) -> None:
        driver = _make_driver(state=SystemState.EMERGENCY)
        driver.check_intent(OrderIntent.INCREASE_RISK)
        metrics = get_fsm_metrics()
        assert metrics.action_blocked[("EMERGENCY", "INCREASE_RISK")] == 1

    def test_allowed_does_not_emit_blocked_metric(self) -> None:
        driver = _make_driver(state=SystemState.ACTIVE)
        driver.check_intent(OrderIntent.INCREASE_RISK)
        metrics = get_fsm_metrics()
        assert len(metrics.action_blocked) == 0

    def test_all_blocked_in_init(self) -> None:
        driver = _make_driver(state=SystemState.INIT)
        for intent in OrderIntent:
            assert driver.check_intent(intent) is False


# ===========================================================================
# 5. Property: state
# ===========================================================================


class TestDriverState:
    """FsmDriver.state reflects current FSM state."""

    def test_initial_state(self) -> None:
        driver = _make_driver(state=SystemState.THROTTLED)
        assert driver.state == SystemState.THROTTLED

    def test_state_updates_after_step(self) -> None:
        reset_fsm_metrics()
        driver = _make_driver(state=SystemState.INIT)
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert driver.state == SystemState.READY


# ===========================================================================
# 6. Evidence artifacts via FsmDriver.step()
# ===========================================================================


class TestDriverEvidence:
    """FsmDriver.step() writes evidence on transition when env enabled."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_evidence_written_on_transition_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv(ENV_ARTIFACT_DIR, str(tmp_path))

        driver = _make_driver(state=SystemState.ACTIVE, enter_ts=_BASE_TS)
        # kill switch -> EMERGENCY transition
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=True,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )

        fsm_dir = tmp_path / "fsm"
        txt_files = list(fsm_dir.glob("*.txt"))
        sha_files = list(fsm_dir.glob("*.sha256"))
        assert len(txt_files) == 1
        assert len(sha_files) == 1
        assert "ACTIVE" in txt_files[0].name
        assert "EMERGENCY" in txt_files[0].name

    def test_no_evidence_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(ENV_ENABLE, raising=False)
        monkeypatch.setenv(ENV_ARTIFACT_DIR, str(tmp_path))

        driver = _make_driver(state=SystemState.ACTIVE, enter_ts=_BASE_TS)
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=True,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )

        fsm_dir = tmp_path / "fsm"
        assert not fsm_dir.exists() or list(fsm_dir.glob("*")) == []

    def test_no_evidence_when_no_transition(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENABLE, "1")
        monkeypatch.setenv(ENV_ARTIFACT_DIR, str(tmp_path))

        driver = _make_driver(state=SystemState.ACTIVE, enter_ts=_BASE_TS)
        # No transition: ACTIVE stays ACTIVE with safe signals
        driver.step(
            ts_ms=_BASE_TS,
            kill_switch_active=False,
            drawdown_breached=False,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )

        fsm_dir = tmp_path / "fsm"
        assert not fsm_dir.exists() or list(fsm_dir.glob("*")) == []
