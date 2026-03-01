"""Integration test: engine emergency exit wiring (RISK-EE-1).

Proves the full chain:
  drawdown_breached → FSM EMERGENCY → executor fires → position_reduced=True → FSM PAUSED

Uses NoOpExchangePort (has cancel_all_orders/place_market_order/get_positions stubs).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import SystemState
from grinder.execution.port import NoOpExchangePort
from grinder.execution.sor_metrics import reset_sor_metrics
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import LiveEngineV0
from grinder.live.fsm_driver import FsmDriver
from grinder.live.fsm_metrics import reset_fsm_metrics
from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorFSM
from grinder.risk.drawdown_guard_v1 import DrawdownGuardV1, DrawdownGuardV1Config
from grinder.risk.emergency_exit_metrics import reset_emergency_exit_metrics


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset global metrics and env vars before each test."""
    reset_fsm_metrics()
    reset_sor_metrics()
    reset_emergency_exit_metrics()
    # Clear env vars that other tests may leak
    monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)
    monkeypatch.delenv("GRINDER_EMERGENCY_EXIT_ENABLED", raising=False)


@pytest.fixture
def _enable_emergency_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set GRINDER_EMERGENCY_EXIT_ENABLED=1."""
    monkeypatch.setenv("GRINDER_EMERGENCY_EXIT_ENABLED", "1")


def _make_snapshot(ts: int) -> Snapshot:
    """Create a minimal Snapshot."""
    return Snapshot(
        ts=ts,
        symbol="BTCUSDT",
        bid_price=Decimal("50000"),
        ask_price=Decimal("50001"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal("50000.5"),
        last_qty=Decimal("0.5"),
    )


def _make_engine(
    port: NoOpExchangePort,
    drawdown_guard: DrawdownGuardV1,
    fsm_driver: FsmDriver,
    *,
    symbol_whitelist: list[str] | None = None,
) -> LiveEngineV0:
    """Build LiveEngineV0 with FSM + DD guard + emergency exit."""
    paper_engine = MagicMock()
    paper_engine.process_snapshot.return_value = MagicMock(actions=[])

    config = LiveEngineConfig(
        armed=True,
        mode=SafeMode.LIVE_TRADE,
        symbol_whitelist=symbol_whitelist or ["BTCUSDT"],
    )

    return LiveEngineV0(
        paper_engine=paper_engine,
        exchange_port=port,
        config=config,
        drawdown_guard=drawdown_guard,
        fsm_driver=fsm_driver,
    )


def _advance_fsm_to_active(engine: LiveEngineV0, start_ts: int) -> int:
    """Tick engine enough to move FSM from INIT → READY → ACTIVE.

    Returns the next usable timestamp.
    """
    # Tick 1: INIT → READY
    engine.process_snapshot(_make_snapshot(start_ts))
    # Tick 2: READY → ACTIVE
    ts2 = start_ts + 1000
    engine.process_snapshot(_make_snapshot(ts2))
    return ts2 + 1000


class TestEngineEmergencyExit:
    """Engine-level integration: drawdown → EMERGENCY → exit → PAUSED."""

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_executor_created_when_enabled(self) -> None:
        """GRINDER_EMERGENCY_EXIT_ENABLED=1 + NoOp port → executor created."""
        port = NoOpExchangePort()
        dd_config = DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05"))
        dd_guard = DrawdownGuardV1(dd_config)
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        engine = _make_engine(port, dd_guard, fsm_driver)
        assert engine._emergency_exit_executor is not None

    def test_executor_not_created_when_disabled(self) -> None:
        """Default (no env var) → executor is None."""
        port = NoOpExchangePort()
        dd_config = DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05"))
        dd_guard = DrawdownGuardV1(dd_config)
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        engine = _make_engine(port, dd_guard, fsm_driver)
        assert engine._emergency_exit_executor is None

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_full_chain_dd_to_paused(self) -> None:
        """drawdown → EMERGENCY → executor runs → position_reduced → PAUSED.

        NoOp port has no real positions, so executor sees 0 positions
        and returns success=True immediately. This proves the wiring chain:
        FSM enters EMERGENCY on drawdown, engine fires executor, executor
        sets position_reduced=True, next tick FSM transitions to PAUSED.
        """
        port = NoOpExchangePort()
        dd_config = DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05"))
        dd_guard = DrawdownGuardV1(dd_config)
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        engine = _make_engine(port, dd_guard, fsm_driver, symbol_whitelist=["BTCUSDT"])

        # Advance FSM to ACTIVE
        ts = _advance_fsm_to_active(engine, 1_000_000)
        assert fsm_driver.state == SystemState.ACTIVE

        # Trigger drawdown breach
        dd_guard.update(equity_current=Decimal("90"), equity_start=Decimal("100"))
        assert dd_guard.is_drawdown  # 10% > 5% limit

        # Tick: ACTIVE → EMERGENCY, executor fires
        engine.process_snapshot(_make_snapshot(ts))
        assert engine._emergency_exit_executed
        assert engine._position_reduced  # NoOp port → 0 positions → success

        # State should still be EMERGENCY after this tick (two-tick transition)
        # because position_reduced is consumed on the NEXT FSM tick
        assert fsm_driver.state == SystemState.EMERGENCY  # type: ignore[comparison-overlap]

        # Next tick: FSM sees position_reduced=True → PAUSED
        # _eval_emergency requires: position_reduced AND NOT drawdown_breached AND NOT kill_switch
        # DrawdownGuardV1 is latched (stays DRAWDOWN until reset), so reset it.
        dd_guard.reset()
        assert not dd_guard.is_drawdown

        ts += 1000
        engine.process_snapshot(_make_snapshot(ts))
        assert fsm_driver.state == SystemState.PAUSED

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_executor_runs_at_most_once(self) -> None:
        """Latch: executor fires once, second EMERGENCY tick is a no-op."""
        port = NoOpExchangePort()
        dd_config = DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05"))
        dd_guard = DrawdownGuardV1(dd_config)
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        engine = _make_engine(port, dd_guard, fsm_driver, symbol_whitelist=["BTCUSDT"])

        # Advance to ACTIVE, trigger drawdown
        ts = _advance_fsm_to_active(engine, 1_000_000)
        dd_guard.update(equity_current=Decimal("90"), equity_start=Decimal("100"))

        # First EMERGENCY tick: executor fires
        engine.process_snapshot(_make_snapshot(ts))
        assert engine._emergency_exit_executed
        first_position_reduced = engine._position_reduced

        # Second EMERGENCY tick: latch prevents re-execution
        ts += 1000
        engine.process_snapshot(_make_snapshot(ts))
        # Still the same state — no re-execution
        assert engine._emergency_exit_executed
        assert engine._position_reduced == first_position_reduced
