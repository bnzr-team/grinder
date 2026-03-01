"""Tests for FSM signal wiring (PR-A1, updated PR-A2a).

Verifies that LiveEngineV0 computes real feed_gap_ms and spread_bps /
toxicity_score_bps signals from snapshot data and ToxicityGate.

PR-A2a changes: engine passes raw numerics to FSM driver instead of
bool/str surrogates. Thresholds now live in FsmConfig (not engine).

Key invariants:
- feed_gap_ms tracked per-symbol (dict[str, int], not single int)
- First tick per symbol always feed_gap_ms=0 (prev_ts == 0)
- All timestamps in milliseconds (Snapshot.ts contract)
- record_price() called exactly once per snapshot (no double-recording)
- No FSM driver + no toxicity gate = zero behavior change (backward compat)
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import SystemState
from grinder.gating.toxicity_gate import ToxicityGate
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.live.fsm_driver import FsmDriver
from grinder.live.fsm_metrics import reset_fsm_metrics
from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorFSM


def _make_snapshot(
    ts: int = 1_000_000,
    symbol: str = "BTCUSDT",
    bid: str = "50000",
    ask: str = "50001",
) -> Snapshot:
    """Helper: create a Snapshot with deterministic defaults (ms timestamps)."""
    return Snapshot(
        ts=ts,
        symbol=symbol,
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_qty=Decimal("1"),
        ask_qty=Decimal("1"),
        last_price=(Decimal(bid) + Decimal(ask)) / 2,
        last_qty=Decimal("0.5"),
    )


def _make_engine(
    *,
    fsm_driver: FsmDriver | None = None,
    toxicity_gate: ToxicityGate | None = None,
) -> LiveEngineV0:
    """Helper: create a minimal LiveEngineV0 with FSM + toxicity wiring.

    Feed stale threshold now lives in FsmConfig (passed to OrchestratorFSM at
    construction). Engine no longer owns the threshold (PR-A2a).
    """
    paper = MagicMock()
    paper.process_snapshot.return_value = MagicMock(actions=[])
    port = MagicMock()
    port.calls = []
    config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
    engine = LiveEngineV0(
        paper_engine=paper,
        exchange_port=port,
        config=config,
        fsm_driver=fsm_driver,
        toxicity_gate=toxicity_gate,
    )
    return engine


# ---------------------------------------------------------------------------
# A) feed_stale signal tests
# ---------------------------------------------------------------------------


class TestFeedStaleSignal:
    """Tests for per-symbol feed staleness detection."""

    def setup_method(self) -> None:
        reset_fsm_metrics()
        os.environ.pop("GRINDER_OPERATOR_OVERRIDE", None)

    def test_feed_stale_false_on_first_tick(self) -> None:
        """First tick for a symbol has no previous ts → feed_stale=False."""
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver)

        snap = _make_snapshot(ts=5000, symbol="BTCUSDT")
        engine.process_snapshot(snap)

        # FSM stays ACTIVE (feed_stale=False on first tick)
        assert driver.state == SystemState.ACTIVE

    def test_feed_stale_false_when_gap_within_threshold(self) -> None:
        """Gap (1s) < threshold (5s) → feed_gap_ms=1000, below FsmConfig threshold."""
        fsm_config = FsmConfig(feed_stale_threshold_ms=5000)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=fsm_config)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver)

        engine.process_snapshot(_make_snapshot(ts=10_000, symbol="BTCUSDT"))
        engine.process_snapshot(_make_snapshot(ts=11_000, symbol="BTCUSDT"))  # 1s gap

        assert driver.state == SystemState.ACTIVE

    def test_feed_stale_true_when_gap_exceeds_threshold(self) -> None:
        """Gap (10s) > threshold (5s) → feed_gap_ms=10000 > FsmConfig threshold → DEGRADED."""
        fsm_config = FsmConfig(feed_stale_threshold_ms=5000)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=fsm_config)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver)

        engine.process_snapshot(_make_snapshot(ts=10_000, symbol="BTCUSDT"))
        engine.process_snapshot(_make_snapshot(ts=20_000, symbol="BTCUSDT"))  # 10s gap

        assert driver.state == SystemState.DEGRADED

    def test_feed_stale_per_symbol_isolation(self) -> None:
        """BTCUSDT stale but ETHUSDT fresh → only BTC tick triggers DEGRADED."""
        fsm_config = FsmConfig(feed_stale_threshold_ms=5000)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=fsm_config)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver)

        # Tick 1: both symbols at t=10s
        engine.process_snapshot(_make_snapshot(ts=10_000, symbol="BTCUSDT"))
        engine.process_snapshot(_make_snapshot(ts=10_000, symbol="ETHUSDT"))
        assert driver.state == SystemState.ACTIVE

        # Tick 2: ETH at t=11s (fresh), BTC at t=20s (10s gap → stale)
        engine.process_snapshot(_make_snapshot(ts=11_000, symbol="ETHUSDT"))
        assert driver.state == SystemState.ACTIVE  # ETH fresh → no change

        engine.process_snapshot(_make_snapshot(ts=20_000, symbol="BTCUSDT"))
        assert driver.state == SystemState.DEGRADED  # type: ignore[comparison-overlap]

    def test_fsm_transitions_to_degraded_on_stale(self) -> None:
        """Full chain: stale feed → DEGRADED, then recover → READY.

        FSM cooldown_ms defaults to 30_000ms. Recovery requires:
        (1) feed_gap_ms below threshold AND (2) cooldown elapsed.
        """
        fsm_config = FsmConfig(cooldown_ms=5000, feed_stale_threshold_ms=5000)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=fsm_config)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver)

        # Establish baseline
        engine.process_snapshot(_make_snapshot(ts=10_000))
        assert driver.state == SystemState.ACTIVE

        # Stale gap (10s > 5s threshold)
        engine.process_snapshot(_make_snapshot(ts=20_000))
        assert driver.state == SystemState.DEGRADED  # type: ignore[comparison-overlap]

        # Recovery tick: non-stale gap (1s) but cooldown not elapsed yet
        engine.process_snapshot(_make_snapshot(ts=21_000))  # 1s gap → not stale
        assert driver.state == SystemState.DEGRADED  # cooldown: 1s < 5s

        # After cooldown: non-stale gap (1s) + cooldown elapsed (6s > 5s)
        engine.process_snapshot(_make_snapshot(ts=22_000))  # update prev_ts
        engine.process_snapshot(_make_snapshot(ts=23_000))  # update prev_ts
        engine.process_snapshot(_make_snapshot(ts=24_000))  # update prev_ts
        engine.process_snapshot(_make_snapshot(ts=26_000))  # 2s gap, cooldown: 6s > 5s
        assert driver.state == SystemState.READY


# ---------------------------------------------------------------------------
# B) toxicity_level signal tests
# ---------------------------------------------------------------------------


class TestToxicitySignal:
    """Tests for ToxicityGate → toxicity_level mapping."""

    def setup_method(self) -> None:
        reset_fsm_metrics()
        os.environ.pop("GRINDER_OPERATOR_OVERRIDE", None)

    def test_toxicity_low_when_no_gate(self) -> None:
        """No ToxicityGate → toxicity_level always "LOW" (backward compat)."""
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver, toxicity_gate=None)

        # Process many ticks — FSM stays ACTIVE (LOW → no transition)
        for t in range(10_000, 15_000, 1000):
            engine.process_snapshot(_make_snapshot(ts=t))

        assert driver.state == SystemState.ACTIVE

    def test_toxicity_mid_on_spread_spike(self) -> None:
        """Wide spread → SPREAD_SPIKE → toxicity "MID" → FSM THROTTLED."""
        gate = ToxicityGate(max_spread_bps=50.0, max_price_impact_bps=500.0)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver, toxicity_gate=gate)

        # Establish non-stale baseline
        engine.process_snapshot(_make_snapshot(ts=10_000))
        assert driver.state == SystemState.ACTIVE

        # Wide spread: (50200-49800)/50000*10000 = 80 bps > 50 bps threshold
        snap = _make_snapshot(ts=11_000, bid="49800", ask="50200")
        engine.process_snapshot(snap)

        # toxicity_level="MID" → THROTTLED (from ACTIVE)
        assert driver.state == SystemState.THROTTLED  # type: ignore[comparison-overlap]

    def test_toxicity_high_on_price_impact(self) -> None:
        """Rapid price move → toxicity_score_bps > threshold → FSM PAUSED.

        PR-A2a: engine passes raw price_impact_bps() to FSM. The FSM config
        threshold (100.0 bps here) determines what counts as HIGH.
        """
        gate = ToxicityGate(lookback_window_ms=5000)
        # FSM config: 100 bps threshold so 2% move (200 bps) triggers HIGH
        fsm_config = FsmConfig(toxicity_high_threshold_bps=100.0)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=fsm_config)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver, toxicity_gate=gate)

        # Baseline at 50000
        engine.process_snapshot(_make_snapshot(ts=10_000, bid="50000", ask="50001"))
        assert driver.state == SystemState.ACTIVE

        # Price jumps to 51000 (2% move, > 1% threshold, narrow spread)
        engine.process_snapshot(_make_snapshot(ts=11_000, bid="51000", ask="51001"))

        # toxicity_level="HIGH" → PAUSED (from ACTIVE)
        assert driver.state == SystemState.PAUSED  # type: ignore[comparison-overlap]

    def test_fsm_transitions_on_toxicity_recovery(self) -> None:
        """Toxic → THROTTLED, then normal spreads → recover to ACTIVE.

        Must keep tick gaps below feed_stale threshold (5s) to avoid
        DEGRADED transition masking toxicity recovery.
        """
        fsm_config = FsmConfig(cooldown_ms=5000)  # short cooldown for test
        gate = ToxicityGate(max_spread_bps=50.0, max_price_impact_bps=500.0)
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=fsm_config)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver, toxicity_gate=gate)

        # Baseline
        engine.process_snapshot(_make_snapshot(ts=10_000))
        assert driver.state == SystemState.ACTIVE

        # Toxic: (50200-49800)/50000*10000 = 80 bps > 50 bps threshold
        engine.process_snapshot(_make_snapshot(ts=11_000, bid="49800", ask="50200"))
        assert driver.state == SystemState.THROTTLED  # type: ignore[comparison-overlap]

        # Recovery: normal spread, keep gaps < 5s to avoid feed_stale
        # Need to reach cooldown: 5s from state_enter=11000 → need ts >= 16000
        engine.process_snapshot(_make_snapshot(ts=13_000))  # 2s gap, still THROTTLED
        engine.process_snapshot(_make_snapshot(ts=15_000))  # 2s gap, still in cooldown
        engine.process_snapshot(_make_snapshot(ts=17_000))  # 2s gap, cooldown elapsed (6s > 5s)
        assert driver.state == SystemState.ACTIVE


# ---------------------------------------------------------------------------
# C) Backward compatibility + guard tests
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Tests proving zero behavior change when FSM disabled."""

    def setup_method(self) -> None:
        reset_fsm_metrics()
        os.environ.pop("GRINDER_OPERATOR_OVERRIDE", None)

    def test_no_fsm_driver_no_behavior_change(self) -> None:
        """Engine with no FSM driver + no toxicity gate = works exactly as before."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        port = MagicMock()
        port.calls = []
        config = LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY)
        engine = LiveEngineV0(
            paper_engine=paper,
            exchange_port=port,
            config=config,
            # No fsm_driver, no toxicity_gate
        )

        # Process snapshots — no errors, no FSM state to check
        for t in range(10_000, 20_000, 1000):
            output = engine.process_snapshot(_make_snapshot(ts=t))
            assert output is not None  # returns normally

    def test_record_price_called_exactly_once_per_snapshot(self) -> None:
        """On one snapshot → record_price() called exactly 1 time (no double-recording)."""
        gate = ToxicityGate()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver, toxicity_gate=gate)

        with patch.object(gate, "record_price", wraps=gate.record_price) as spy:
            engine.process_snapshot(_make_snapshot(ts=10_000, symbol="BTCUSDT"))
            assert spy.call_count == 1
            # Verify correct args (ts=10000, symbol="BTCUSDT", mid_price)
            args = spy.call_args
            assert args[0][0] == 10_000  # ts
            assert args[0][1] == "BTCUSDT"  # symbol

        # Second snapshot
        with patch.object(gate, "record_price", wraps=gate.record_price) as spy:
            engine.process_snapshot(_make_snapshot(ts=11_000, symbol="ETHUSDT"))
            assert spy.call_count == 1
            assert spy.call_args[0][1] == "ETHUSDT"

    def test_toxicity_gate_price_impact_bps_uses_snapshot_data(self) -> None:
        """ToxicityGate.price_impact_bps() receives correct snapshot fields (PR-A2a)."""
        gate = ToxicityGate()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)
        engine = _make_engine(fsm_driver=driver, toxicity_gate=gate)

        snap = _make_snapshot(ts=10_000, symbol="BTCUSDT", bid="50000", ask="50001")

        with patch.object(gate, "price_impact_bps", wraps=gate.price_impact_bps) as spy:
            engine.process_snapshot(snap)
            assert spy.call_count == 1
            args = spy.call_args[0]
            assert args[0] == 10_000  # ts_ms
            assert args[1] == "BTCUSDT"  # symbol
            assert isinstance(args[2], Decimal)  # mid_price
