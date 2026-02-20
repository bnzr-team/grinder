"""Tests for FsmMetrics (Launch-13 PR2).

Validates:
- One-hot current_state gauge emits all 7 SystemState values
- Counters increment correctly (transitions, action_blocked)
- Prometheus output format (HELP/TYPE headers present)
- Contract alignment: output contains all grinder_fsm_* patterns from SSOT
"""

from __future__ import annotations

from grinder.core import SystemState
from grinder.live.fsm_metrics import (
    METRIC_FSM_ACTION_BLOCKED,
    METRIC_FSM_CURRENT_STATE,
    METRIC_FSM_STATE_DURATION,
    METRIC_FSM_TRANSITIONS,
    FsmMetrics,
    get_fsm_metrics,
    reset_fsm_metrics,
)
from grinder.live.fsm_orchestrator import TransitionEvent, TransitionReason
from grinder.observability.metrics_contract import REQUIRED_METRICS_PATTERNS
from grinder.risk.drawdown_guard_v1 import OrderIntent


class TestOneHotCurrentState:
    """True one-hot encoding: all 7 states emitted every build."""

    def test_all_states_present_when_set(self) -> None:
        """After set_current_state, output has all 7 states with exactly one '1'."""
        m = FsmMetrics()
        m.set_current_state(SystemState.ACTIVE)
        lines = "\n".join(m.to_prometheus_lines())

        for state in SystemState:
            key = f'grinder_fsm_current_state{{state="{state.name}"}}'
            assert key in lines, f"Missing state line: {key}"

        # Exactly one state should be 1
        ones = [s for s in SystemState if f'{{state="{s.name}"}} 1' in lines]
        zeros = [s for s in SystemState if f'{{state="{s.name}"}} 0' in lines]
        assert ones == [SystemState.ACTIVE]
        assert len(zeros) == 6

    def test_all_states_present_when_none_set(self) -> None:
        """Before any set_current_state, all states should be 0."""
        m = FsmMetrics()
        lines = "\n".join(m.to_prometheus_lines())

        for state in SystemState:
            key = f'grinder_fsm_current_state{{state="{state.name}"}} 0'
            assert key in lines, f"Missing zero state: {key}"

    def test_state_derived_from_enum(self) -> None:
        """State list comes from SystemState enum, not hardcoded."""
        m = FsmMetrics()
        m.set_current_state(SystemState.EMERGENCY)
        lines = m.to_prometheus_lines()
        state_lines = [ln for ln in lines if ln.startswith(METRIC_FSM_CURRENT_STATE + "{")]
        assert len(state_lines) == len(SystemState)


class TestTransitionsCounter:
    """Transition counter increments correctly."""

    def test_record_transition_increments(self) -> None:
        m = FsmMetrics()
        event = TransitionEvent(
            ts_ms=1000,
            from_state=SystemState.INIT,
            to_state=SystemState.READY,
            reason=TransitionReason.HEALTH_OK,
        )
        m.record_transition(event)
        assert m.transitions[("INIT", "READY", "HEALTH_OK")] == 1

        m.record_transition(event)
        assert m.transitions[("INIT", "READY", "HEALTH_OK")] == 2

    def test_transitions_in_prometheus_output(self) -> None:
        m = FsmMetrics()
        event = TransitionEvent(
            ts_ms=1000,
            from_state=SystemState.ACTIVE,
            to_state=SystemState.THROTTLED,
            reason=TransitionReason.TOX_MID,
        )
        m.record_transition(event)
        lines = "\n".join(m.to_prometheus_lines())
        assert 'from_state="ACTIVE"' in lines
        assert 'to_state="THROTTLED"' in lines
        assert 'reason="TOX_MID"' in lines

    def test_empty_transitions_placeholder(self) -> None:
        m = FsmMetrics()
        lines = "\n".join(m.to_prometheus_lines())
        assert 'from_state="none"' in lines


class TestActionBlockedCounter:
    """Action blocked counter increments correctly."""

    def test_record_action_blocked_increments(self) -> None:
        m = FsmMetrics()
        m.record_action_blocked(SystemState.PAUSED, OrderIntent.INCREASE_RISK)
        assert m.action_blocked[("PAUSED", "INCREASE_RISK")] == 1

        m.record_action_blocked(SystemState.PAUSED, OrderIntent.INCREASE_RISK)
        assert m.action_blocked[("PAUSED", "INCREASE_RISK")] == 2

    def test_action_blocked_in_prometheus_output(self) -> None:
        m = FsmMetrics()
        m.record_action_blocked(SystemState.EMERGENCY, OrderIntent.INCREASE_RISK)
        lines = "\n".join(m.to_prometheus_lines())
        assert 'state="EMERGENCY"' in lines
        assert 'intent="INCREASE_RISK"' in lines


class TestStateDuration:
    """State duration gauge."""

    def test_set_state_duration(self) -> None:
        m = FsmMetrics()
        m.set_state_duration(42.5)
        lines = "\n".join(m.to_prometheus_lines())
        assert "grinder_fsm_state_duration_seconds 42.50" in lines


class TestPrometheusFormat:
    """Prometheus output format correctness."""

    def test_help_and_type_headers_present(self) -> None:
        """All 4 metrics have HELP + TYPE headers."""
        m = FsmMetrics()
        lines = "\n".join(m.to_prometheus_lines())

        for metric_name in [
            METRIC_FSM_CURRENT_STATE,
            METRIC_FSM_STATE_DURATION,
            METRIC_FSM_TRANSITIONS,
            METRIC_FSM_ACTION_BLOCKED,
        ]:
            assert f"# HELP {metric_name}" in lines, f"Missing HELP for {metric_name}"
            assert f"# TYPE {metric_name}" in lines, f"Missing TYPE for {metric_name}"

    def test_deterministic_ordering(self) -> None:
        """Counter keys are sorted; state lines follow enum order."""
        m = FsmMetrics()
        m.set_current_state(SystemState.INIT)
        # Add transitions in reverse order
        for reason in [TransitionReason.TOX_MID, TransitionReason.HEALTH_OK]:
            m.record_transition(
                TransitionEvent(
                    ts_ms=1000,
                    from_state=SystemState.INIT,
                    to_state=SystemState.READY,
                    reason=reason,
                )
            )
        lines = m.to_prometheus_lines()
        # State lines should be in enum order
        state_lines = [ln for ln in lines if ln.startswith(METRIC_FSM_CURRENT_STATE + "{")]
        state_names = [ln.split('"')[1] for ln in state_lines]
        assert state_names == [s.name for s in SystemState]


class TestContractAlignment:
    """FSM metrics output matches REQUIRED_METRICS_PATTERNS for grinder_fsm_*."""

    def test_all_fsm_contract_patterns_present(self) -> None:
        """Fresh FsmMetrics output contains all grinder_fsm_* contract patterns."""
        m = FsmMetrics()
        m.set_current_state(SystemState.INIT)
        output = "\n".join(m.to_prometheus_lines())

        fsm_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "grinder_fsm_" in p]
        assert len(fsm_patterns) > 0, "No grinder_fsm_ patterns in REQUIRED_METRICS_PATTERNS"

        missing = [p for p in fsm_patterns if p not in output]
        assert missing == [], f"Missing contract patterns: {missing}"


class TestSingleton:
    """Global singleton get/reset."""

    def test_get_returns_same_instance(self) -> None:
        reset_fsm_metrics()
        m1 = get_fsm_metrics()
        m2 = get_fsm_metrics()
        assert m1 is m2

    def test_reset_creates_new_instance(self) -> None:
        reset_fsm_metrics()
        m1 = get_fsm_metrics()
        reset_fsm_metrics()
        m2 = get_fsm_metrics()
        assert m1 is not m2
