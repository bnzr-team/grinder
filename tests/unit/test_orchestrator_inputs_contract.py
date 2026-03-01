"""Contract tests for OrchestratorInputs numeric type alignment (PR-A2a).

Validates:
- FsmConfig defaults match prior hardcoded values (meta-test)
- build_inputs() rejects negative numeric fields
- FSM boundary behavior reads thresholds from FsmConfig (not magic numbers)
- ToxicityGate.price_impact_bps() typed method (deterministic, read-only)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.core import SystemState
from grinder.gating.toxicity_gate import ToxicityGate
from grinder.live.fsm_driver import build_inputs
from grinder.live.fsm_orchestrator import (
    FsmConfig,
    OrchestratorFSM,
    OrchestratorInputs,
    TransitionReason,
)

# ===========================================================================
# 1. Meta-test: FsmConfig defaults match prior hardcodes
# ===========================================================================


class TestFsmConfigDefaults:
    """Prove FsmConfig defaults reproduce prior behavior exactly."""

    def test_fsm_config_defaults_match_prior_hardcodes(self) -> None:
        config = FsmConfig()
        assert config.feed_stale_threshold_ms == 5_000  # was engine GRINDER_FEED_STALE_MS default
        assert config.spread_spike_threshold_bps == 50.0  # was ToxicityGate.max_spread_bps
        assert config.toxicity_high_threshold_bps == 500.0  # was ToxicityGate.max_price_impact_bps
        assert config.drawdown_threshold_pct == 0.20  # was DrawdownGuardV1Config default
        assert config.cooldown_ms == 30_000  # unchanged


# ===========================================================================
# 2. Golden fixture: valid OrchestratorInputs
# ===========================================================================


class TestOrchestratorInputsGolden:
    """Valid construction of OrchestratorInputs."""

    def test_default_numeric_values_construct(self) -> None:
        inp = OrchestratorInputs(
            ts_ms=1000,
            kill_switch_active=False,
            drawdown_pct=0.0,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert inp.feed_gap_ms == 0
        assert inp.spread_bps == 0.0
        assert inp.toxicity_score_bps == 0.0

    def test_nonzero_numeric_values_construct(self) -> None:
        inp = OrchestratorInputs(
            ts_ms=2000,
            kill_switch_active=False,
            drawdown_pct=0.0,
            feed_gap_ms=10_000,
            spread_bps=80.0,
            toxicity_score_bps=600.0,
            position_reduced=False,
            operator_override=None,
        )
        assert inp.feed_gap_ms == 10_000
        assert inp.spread_bps == 80.0
        assert inp.toxicity_score_bps == 600.0


# ===========================================================================
# 3. Negative tests: build_inputs() rejects invalid values
# ===========================================================================


class TestBuildInputsValidation:
    """build_inputs() rejects negative numeric fields."""

    def test_negative_drawdown_pct_raises(self) -> None:
        with pytest.raises(ValueError, match="drawdown_pct must be >= 0"):
            build_inputs(
                ts_ms=1000,
                kill_switch_active=False,
                drawdown_pct=-0.01,
                feed_gap_ms=0,
                spread_bps=0.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_negative_feed_gap_ms_raises(self) -> None:
        with pytest.raises(ValueError, match="feed_gap_ms must be >= 0"):
            build_inputs(
                ts_ms=1000,
                kill_switch_active=False,
                drawdown_pct=0.0,
                feed_gap_ms=-1,
                spread_bps=0.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_negative_spread_bps_raises(self) -> None:
        with pytest.raises(ValueError, match="spread_bps must be >= 0"):
            build_inputs(
                ts_ms=1000,
                kill_switch_active=False,
                drawdown_pct=0.0,
                feed_gap_ms=0,
                spread_bps=-1.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_negative_toxicity_score_bps_raises(self) -> None:
        with pytest.raises(ValueError, match="toxicity_score_bps must be >= 0"):
            build_inputs(
                ts_ms=1000,
                kill_switch_active=False,
                drawdown_pct=0.0,
                feed_gap_ms=0,
                spread_bps=0.0,
                toxicity_score_bps=-1.0,
                position_reduced=False,
                operator_override=None,
            )

    def test_invalid_operator_override_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid operator_override"):
            build_inputs(
                ts_ms=1000,
                kill_switch_active=False,
                drawdown_pct=0.0,
                feed_gap_ms=0,
                spread_bps=0.0,
                toxicity_score_bps=0.0,
                position_reduced=False,
                operator_override="INVALID",
            )


# ===========================================================================
# 4. Boundary tests: FSM thresholds from FsmConfig
# ===========================================================================


def _safe_inputs(
    ts_ms: int = 2000,
    feed_gap_ms: int = 0,
    spread_bps: float = 0.0,
    toxicity_score_bps: float = 0.0,
) -> OrchestratorInputs:
    """Helper: minimal safe inputs for boundary tests."""
    return OrchestratorInputs(
        ts_ms=ts_ms,
        kill_switch_active=False,
        drawdown_pct=0.0,
        feed_gap_ms=feed_gap_ms,
        spread_bps=spread_bps,
        toxicity_score_bps=toxicity_score_bps,
        position_reduced=False,
        operator_override=None,
    )


class TestFsmBoundaryFromConfig:
    """Boundary tests read thresholds from FsmConfig, not magic numbers."""

    def test_feed_stale_boundary_from_config(self) -> None:
        config = FsmConfig()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=config)

        # Just at threshold → NOT stale (> required, not >=)
        inp_at = _safe_inputs(ts_ms=2000, feed_gap_ms=config.feed_stale_threshold_ms)
        assert fsm.tick(inp_at) is None

        # Just above threshold → DEGRADED
        inp_above = _safe_inputs(ts_ms=3000, feed_gap_ms=config.feed_stale_threshold_ms + 1)
        event = fsm.tick(inp_above)
        assert event is not None
        assert event.to_state == SystemState.DEGRADED

    def test_spread_spike_boundary_from_config(self) -> None:
        config = FsmConfig()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=config)

        # Just at threshold → NOT toxic (> required, not >=)
        inp_at = _safe_inputs(ts_ms=2000, spread_bps=config.spread_spike_threshold_bps)
        assert fsm.tick(inp_at) is None

        # Just above threshold → THROTTLED (MID toxicity)
        inp_above = _safe_inputs(ts_ms=3000, spread_bps=config.spread_spike_threshold_bps + 0.1)
        event = fsm.tick(inp_above)
        assert event is not None
        assert event.to_state == SystemState.THROTTLED

    def test_toxicity_high_boundary_from_config(self) -> None:
        config = FsmConfig()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=config)

        # Just at threshold → NOT high (> required, not >=)
        inp_at = _safe_inputs(ts_ms=2000, toxicity_score_bps=config.toxicity_high_threshold_bps)
        assert fsm.tick(inp_at) is None

        # Just above threshold → PAUSED (HIGH toxicity)
        inp_above = _safe_inputs(
            ts_ms=3000, toxicity_score_bps=config.toxicity_high_threshold_bps + 1.0
        )
        event = fsm.tick(inp_above)
        assert event is not None
        assert event.to_state == SystemState.PAUSED

    def test_drawdown_boundary_from_config(self) -> None:
        config = FsmConfig()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=config)

        # Just below threshold → ACTIVE (>= required)
        inp_below = OrchestratorInputs(
            ts_ms=2000,
            kill_switch_active=False,
            drawdown_pct=config.drawdown_threshold_pct - 0.01,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        assert fsm.tick(inp_below) is None

        # At threshold → EMERGENCY (>= triggers)
        inp_at = OrchestratorInputs(
            ts_ms=3000,
            kill_switch_active=False,
            drawdown_pct=config.drawdown_threshold_pct,
            feed_gap_ms=0,
            spread_bps=0.0,
            toxicity_score_bps=0.0,
            position_reduced=False,
            operator_override=None,
        )
        event = fsm.tick(inp_at)
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.DD_BREACH

    def test_feed_gap_zero_is_not_stale(self) -> None:
        """feed_gap_ms=0 (first tick) is never stale, regardless of threshold."""
        config = FsmConfig(feed_stale_threshold_ms=0)  # even zero threshold
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=config)
        inp = _safe_inputs(ts_ms=2000, feed_gap_ms=0)
        assert fsm.tick(inp) is None  # still ACTIVE


# ===========================================================================
# 5. ToxicityGate.price_impact_bps() tests
# ===========================================================================


class TestPriceImpactBps:
    """Tests for typed price_impact_bps() method (deterministic, read-only)."""

    def test_zero_when_no_history(self) -> None:
        gate = ToxicityGate()
        assert gate.price_impact_bps(10_000, "BTCUSDT", Decimal("50000")) == 0.0

    def test_correct_value_single_entry(self) -> None:
        gate = ToxicityGate()
        gate.record_price(10_000, "BTCUSDT", Decimal("50000"))
        # 2% price move = 200 bps
        impact = gate.price_impact_bps(11_000, "BTCUSDT", Decimal("51000"))
        assert abs(impact - 200.0) < 0.1

    def test_oldest_in_window_used_not_newest(self) -> None:
        """Multiple entries: oldest in-window is used (first match in insertion order)."""
        gate = ToxicityGate(lookback_window_ms=5000)
        gate.record_price(10_000, "BTCUSDT", Decimal("50000"))  # oldest
        gate.record_price(11_000, "BTCUSDT", Decimal("50500"))  # newer
        gate.record_price(12_000, "BTCUSDT", Decimal("51000"))  # newest

        # At ts=14000, window_start=9000, all entries in-window
        # Should use oldest (50000), not newest (51000)
        # Impact from 50000→52000 = 4% = 400 bps
        impact = gate.price_impact_bps(14_000, "BTCUSDT", Decimal("52000"))
        assert abs(impact - 400.0) < 0.1

    def test_out_of_window_entries_ignored(self) -> None:
        """Old entries outside lookback window are skipped."""
        gate = ToxicityGate(lookback_window_ms=5000)
        gate.record_price(1_000, "BTCUSDT", Decimal("40000"))  # out of window
        gate.record_price(10_000, "BTCUSDT", Decimal("50000"))  # in window

        # At ts=14000, window_start=9000 → 1000 is out, 10000 is in
        # Impact from 50000→51000 = 2% = 200 bps
        impact = gate.price_impact_bps(14_000, "BTCUSDT", Decimal("51000"))
        assert abs(impact - 200.0) < 0.1

    def test_returns_float_not_decimal(self) -> None:
        gate = ToxicityGate()
        gate.record_price(10_000, "BTCUSDT", Decimal("50000"))
        result = gate.price_impact_bps(11_000, "BTCUSDT", Decimal("51000"))
        assert isinstance(result, float)

    def test_read_only_does_not_mutate_history(self) -> None:
        """price_impact_bps() is read-only: history length unchanged after call."""
        gate = ToxicityGate(lookback_window_ms=5000)
        gate.record_price(1_000, "BTCUSDT", Decimal("50000"))
        gate.record_price(10_000, "BTCUSDT", Decimal("50100"))
        before = gate.prices_in_window("BTCUSDT")
        gate.price_impact_bps(14_000, "BTCUSDT", Decimal("50200"))
        after = gate.prices_in_window("BTCUSDT")
        assert before == after
