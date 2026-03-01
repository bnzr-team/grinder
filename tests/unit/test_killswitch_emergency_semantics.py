"""Contract tests: KillSwitch + EmergencyExit semantics (PR-B).

Verifies existing semantics at main @ 9a98648. No new runtime code.

Existing coverage (test_engine_emergency_exit.py):
  - Full chain: drawdown -> EMERGENCY -> executor fires -> sync 0.0 -> PAUSED
  - Latch: executor runs at most once (engine-level)
  - Executor created/not-created based on env flag

What this file adds (contract/edge cases):
  1. Exit NO-OP outside EMERGENCY (B1: trigger gate)
  2. Latch with executor call-count spy (B2: idempotency)
  3. State transition via confirmed measurement (B3: AccountSyncer, not override)
  4. Metrics contract: 4 metric families present after exit (B4)
  5. KillSwitch priority: config.kill_switch_active -> FSM EMERGENCY (B5)
  6. KillSwitch effect: PLACE blocked, CANCEL allowed (B5)
  7. INIT + kill_switch -> stays INIT (edge case)
  8. Recovery blocked when kill_switch active even if flat (safety)

Source references (main @ 9a98648):
  - Trigger gate: engine.py:436-443 (FSM==EMERGENCY AND not latch)
  - Latch: engine.py:440,558 (_emergency_exit_executed)
  - FSM EMERGENCY recovery: fsm_orchestrator.py:367-381 (_eval_emergency)
  - INIT skip: fsm_orchestrator.py:231 (state != INIT guard)
  - Kill switch -> EMERGENCY: fsm_orchestrator.py:250-251
  - Kill switch blocks recovery: fsm_orchestrator.py:377
  - Gate 3 (PLACE blocked): engine.py:652-664
  - Metrics: emergency_exit_metrics.py:41-71
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from grinder.account.syncer import AccountSyncer
from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide, SystemState
from grinder.execution.port import NoOpExchangePort
from grinder.execution.sor_metrics import reset_sor_metrics
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import BlockReason, LiveActionStatus, LiveEngineV0
from grinder.live.fsm_driver import FsmDriver
from grinder.live.fsm_metrics import reset_fsm_metrics
from grinder.live.fsm_orchestrator import (
    FsmConfig,
    OrchestratorFSM,
    OrchestratorInputs,
    TransitionReason,
)
from grinder.risk.drawdown_guard_v1 import DrawdownGuardV1, DrawdownGuardV1Config
from grinder.risk.emergency_exit import EmergencyExitResult
from grinder.risk.emergency_exit_metrics import (
    get_emergency_exit_metrics,
    reset_emergency_exit_metrics,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset global metrics and env vars before each test."""
    reset_fsm_metrics()
    reset_sor_metrics()
    reset_emergency_exit_metrics()
    monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)
    monkeypatch.delenv("GRINDER_EMERGENCY_EXIT_ENABLED", raising=False)


@pytest.fixture
def _enable_emergency_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set GRINDER_EMERGENCY_EXIT_ENABLED=1."""
    monkeypatch.setenv("GRINDER_EMERGENCY_EXIT_ENABLED", "1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(ts: int) -> Snapshot:
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
    dd_guard: DrawdownGuardV1,
    fsm_driver: FsmDriver,
    *,
    kill_switch_active: bool = False,
    symbol_whitelist: list[str] | None = None,
    account_syncer: AccountSyncer | None = None,
    account_sync_enabled: bool = False,
) -> LiveEngineV0:
    """Build LiveEngineV0 with configurable kill switch."""
    paper_engine = MagicMock()
    paper_engine.process_snapshot.return_value = MagicMock(actions=[])

    config = LiveEngineConfig(
        armed=True,
        mode=SafeMode.LIVE_TRADE,
        kill_switch_active=kill_switch_active,
        symbol_whitelist=symbol_whitelist or ["BTCUSDT"],
    )

    engine = LiveEngineV0(
        paper_engine=paper_engine,
        exchange_port=port,
        config=config,
        drawdown_guard=dd_guard,
        fsm_driver=fsm_driver,
        account_syncer=account_syncer,
    )

    if account_sync_enabled:
        engine._account_sync_env_override = True

    return engine


def _advance_to_active(engine: LiveEngineV0, start_ts: int) -> int:
    """INIT -> READY -> ACTIVE. Returns next usable ts."""
    engine.process_snapshot(_snapshot(start_ts))
    ts2 = start_ts + 1000
    engine.process_snapshot(_snapshot(ts2))
    return ts2 + 1000


def _fsm_inputs(
    *,
    ts_ms: int = 2000,
    kill_switch: bool = False,
    drawdown_pct: float = 0.0,
    position_notional_usd: float | None = 100.0,
) -> OrchestratorInputs:
    """Build OrchestratorInputs with defaults for clean tests."""
    return OrchestratorInputs(
        ts_ms=ts_ms,
        kill_switch_active=kill_switch,
        drawdown_pct=drawdown_pct,
        feed_gap_ms=0,
        spread_bps=0.0,
        toxicity_score_bps=0.0,
        position_notional_usd=position_notional_usd,
        operator_override=None,
    )


# ===========================================================================
# B1: Trigger gate — exit fires ONLY from FSM=EMERGENCY
# ===========================================================================


class TestEmergencyExitTriggerGate:
    """EmergencyExit must NOT execute when FSM is not in EMERGENCY state.

    Source: engine.py:436-443 — condition requires fsm_driver.state == EMERGENCY.
    """

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_emergency_exit_not_triggered_outside_emergency(self) -> None:
        """ACTIVE state + exit enabled + executor exists -> exit does NOT fire.

        Arrange: FSM in ACTIVE, emergency exit enabled, executor exists.
        Act: process_snapshot (FSM stays ACTIVE, no emergency triggers).
        Assert: executor not called, latch not set.
        """
        port = NoOpExchangePort()
        dd_guard = DrawdownGuardV1(DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05")))
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        syncer = AccountSyncer(port)
        engine = _make_engine(
            port,
            dd_guard,
            fsm_driver,
            account_syncer=syncer,
            account_sync_enabled=True,
        )
        assert engine._emergency_exit_executor is not None  # executor exists

        ts = _advance_to_active(engine, 1_000_000)
        assert fsm_driver.state == SystemState.ACTIVE

        # Tick in ACTIVE state — no drawdown breach, no kill switch
        engine.process_snapshot(_snapshot(ts))

        # Exit must NOT have fired
        assert not engine._emergency_exit_executed
        assert fsm_driver.state == SystemState.ACTIVE


# ===========================================================================
# B2: Latch / idempotency — executor fires exactly once
# ===========================================================================


class TestEmergencyExitLatch:
    """Latch: second EMERGENCY tick must NOT re-execute closure.

    Source: engine.py:440 (not self._emergency_exit_executed) guards re-entry.
    """

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_emergency_exit_triggers_once_latch(self) -> None:
        """Two EMERGENCY ticks -> executor.execute() called exactly once.

        Uses patch to spy on executor.execute call count.
        """
        port = NoOpExchangePort()
        dd_guard = DrawdownGuardV1(DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05")))
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        syncer = AccountSyncer(port)
        engine = _make_engine(
            port,
            dd_guard,
            fsm_driver,
            symbol_whitelist=["BTCUSDT"],
            account_syncer=syncer,
            account_sync_enabled=True,
        )

        ts = _advance_to_active(engine, 1_000_000)

        # Trigger drawdown -> EMERGENCY
        dd_guard.update(equity_current=Decimal("90"), equity_start=Decimal("100"))

        # Spy on executor.execute
        assert engine._emergency_exit_executor is not None
        with patch.object(
            engine._emergency_exit_executor,
            "execute",
            wraps=engine._emergency_exit_executor.execute,
        ) as spy:
            # Tick 1: ACTIVE -> EMERGENCY, executor fires
            engine.process_snapshot(_snapshot(ts))
            assert engine._emergency_exit_executed
            assert spy.call_count == 1

            # Tick 2: still EMERGENCY, latch prevents re-execution
            ts += 1000
            engine.process_snapshot(_snapshot(ts))
            assert spy.call_count == 1  # NOT 2


# ===========================================================================
# B3: State transition — confirmed measurement (not override)
# ===========================================================================


class TestEmergencyExitStateTransition:
    """EMERGENCY -> exit -> AccountSyncer confirms flat -> PAUSED.

    Source: fsm_orchestrator.py:367-381 (_eval_emergency).
    Recovery requires: position_notional_usd is not None AND < threshold
    AND not kill_switch AND not dd_breached.

    position_notional_usd comes from AccountSyncer.compute_position_notional(),
    NOT from manual override. This test proves the confirmed measurement path.
    """

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_emergency_exit_state_transition_to_paused_confirmed_measurement(self) -> None:
        """Full chain: drawdown -> EMERGENCY -> exit -> sync confirms 0.0 -> PAUSED.

        AccountSyncer.sync() returns empty snapshot from NoOpExchangePort.
        compute_position_notional(empty snapshot) = 0.0 (confirmed, not override).
        0.0 < 10.0 threshold -> recovery allowed -> FSM PAUSED.
        """
        port = NoOpExchangePort()
        dd_guard = DrawdownGuardV1(DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05")))
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        syncer = AccountSyncer(port)
        engine = _make_engine(
            port,
            dd_guard,
            fsm_driver,
            symbol_whitelist=["BTCUSDT"],
            account_syncer=syncer,
            account_sync_enabled=True,
        )

        ts = _advance_to_active(engine, 1_000_000)

        # Trigger drawdown
        dd_guard.update(equity_current=Decimal("90"), equity_start=Decimal("100"))

        # Tick N: ACTIVE -> EMERGENCY, executor fires, sync runs
        engine.process_snapshot(_snapshot(ts))
        assert engine._emergency_exit_executed
        assert fsm_driver.state == SystemState.EMERGENCY

        # Prove: position_notional_usd set by AccountSyncer (confirmed measurement)
        # NoOp port -> empty positions -> compute_position_notional = 0.0
        assert engine._position_notional_usd == pytest.approx(0.0)

        # Reset DD guard (latched, must reset for recovery)
        dd_guard.reset()
        assert not dd_guard.is_drawdown

        # Tick N+1: FSM sees position_notional_usd=0.0 < 10.0 -> PAUSED
        ts += 1000
        engine.process_snapshot(_snapshot(ts))
        assert fsm_driver.state == SystemState.PAUSED  # type: ignore[comparison-overlap]


# ===========================================================================
# B4: Observability contract — 4 metric families
# ===========================================================================


class TestEmergencyExitMetricsContract:
    """After exit, prometheus output must contain all 4 metric families.

    Exact metric names (from emergency_exit_metrics.py:41-71):
      1. grinder_emergency_exit_enabled (gauge, always emitted)
      2. grinder_emergency_exit_total{result="success"} (counter, after exit)
      3. grinder_emergency_exit_orders_cancelled_total (counter, after exit)
      4. grinder_emergency_exit_positions_closed_total (counter, after exit)
    """

    def test_emergency_exit_metrics_contract(self) -> None:
        """All 4 metric families present in prometheus exposition after exit."""
        metrics = get_emergency_exit_metrics()
        metrics.set_enabled(True)

        # Simulate a successful exit result
        result = EmergencyExitResult(
            triggered_at_ms=1_000_000,
            reason="fsm_emergency",
            orders_cancelled=2,
            market_orders_placed=1,
            positions_remaining=0,
            success=True,
        )
        metrics.record_exit(result)

        lines = "\n".join(metrics.to_prometheus_lines())

        # Family 1: enabled gauge (always emitted)
        assert "grinder_emergency_exit_enabled 1" in lines

        # Family 2: exit counter by result
        assert 'grinder_emergency_exit_total{result="success"} 1' in lines

        # Family 3: orders cancelled counter
        assert "grinder_emergency_exit_orders_cancelled_total 2" in lines

        # Family 4: positions closed counter
        assert "grinder_emergency_exit_positions_closed_total 1" in lines


# ===========================================================================
# B5: KillSwitch semantics
# ===========================================================================


class TestKillSwitchPriority:
    """kill_switch_active in config -> FSM enters EMERGENCY.

    Source: fsm_orchestrator.py:250-251 — _check_emergency checks
    inp.kill_switch_active first (Priority 1).

    kill_switch_active is passed from LiveEngineConfig.kill_switch_active
    to FSM via _tick_fsm (engine.py:511). No separate env var for
    kill_switch — it lives in config only (LiveEngineConfig.kill_switch_active).
    SSOT: config.py:33, DECISIONS.md ADR-036.
    """

    @pytest.mark.usefixtures("_enable_emergency_exit")
    def test_killswitch_priority_env_over_config(self) -> None:
        """kill_switch_active=True in config -> FSM transitions to EMERGENCY.

        Proves: config.kill_switch_active is the SSOT for kill switch state.
        When active, FSM _check_emergency returns EMERGENCY with KILL_SWITCH reason.
        """
        port = NoOpExchangePort()
        dd_guard = DrawdownGuardV1(DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05")))
        fsm = OrchestratorFSM(config=FsmConfig(drawdown_threshold_pct=0.05))
        fsm_driver = FsmDriver(fsm)

        syncer = AccountSyncer(port)
        engine = _make_engine(
            port,
            dd_guard,
            fsm_driver,
            kill_switch_active=True,
            account_syncer=syncer,
            account_sync_enabled=True,
        )

        _advance_to_active(engine, 1_000_000)

        # _eval_init blocks on kill_switch_active (line 295),
        # so FSM stays in INIT. Verify with fresh FSM.
        fsm2 = OrchestratorFSM(config=FsmConfig())
        fsm_driver2 = FsmDriver(fsm2)
        engine2 = _make_engine(
            port,
            dd_guard,
            fsm_driver2,
            kill_switch_active=True,
            account_syncer=syncer,
            account_sync_enabled=True,
        )

        # Tick: INIT + kill_switch -> stays INIT (no EMERGENCY from INIT)
        engine2.process_snapshot(_snapshot(2_000_000))
        assert fsm_driver2.state == SystemState.INIT

        # Now test via FSM directly: from non-INIT state
        fsm3 = OrchestratorFSM(
            state=SystemState.ACTIVE,
            state_enter_ts=0,
            config=FsmConfig(),
        )
        event = fsm3.tick(_fsm_inputs(kill_switch=True))
        assert event is not None
        assert event.to_state == SystemState.EMERGENCY
        assert event.reason == TransitionReason.KILL_SWITCH


class TestKillSwitchEffect:
    """KillSwitch effect on trading operations: PLACE blocked, CANCEL allowed.

    Source: engine.py:652-664 (Gate 3).
    SSOT: config.py:25 ("blocks PLACE/REPLACE but allows CANCEL"),
    DECISIONS.md ADR-036.
    """

    def test_killswitch_disables_trading_place_blocks_cancel_allows(self) -> None:
        """PLACE action -> BLOCKED(KILL_SWITCH_ACTIVE), CANCEL -> not blocked by kill switch.

        Uses engine._process_action directly for deterministic gate testing.
        """
        port = NoOpExchangePort()
        dd_guard = DrawdownGuardV1(DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.05")))
        fsm = OrchestratorFSM(config=FsmConfig())
        fsm_driver = FsmDriver(fsm)

        engine = _make_engine(
            port,
            dd_guard,
            fsm_driver,
            kill_switch_active=True,
        )

        # PLACE action -> blocked by kill switch (Gate 3)
        place_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
        )
        result = engine._process_action(place_action, ts=1_000_000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.KILL_SWITCH_ACTIVE

        # CANCEL action -> NOT blocked by kill switch (Gate 3 allows CANCEL)
        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol="BTCUSDT",
            order_id="test_order_123",
        )
        cancel_result = engine._process_action(cancel_action, ts=1_000_000)
        # CANCEL passes Gate 3 (kill switch). May be blocked by later gates
        # (e.g. FSM state) but NOT by KILL_SWITCH_ACTIVE.
        assert cancel_result.block_reason != BlockReason.KILL_SWITCH_ACTIVE


# ===========================================================================
# Edge cases (promoted to P0)
# ===========================================================================


class TestInitKillSwitchEdgeCase:
    """INIT + kill_switch_active -> stays INIT, does NOT force EMERGENCY.

    Source: fsm_orchestrator.py:231 — _check_emergency skipped when state==INIT.
    _eval_init (line 295): blocks INIT->READY when kill_switch_active.
    """

    def test_init_killswitch_does_not_force_emergency(self) -> None:
        """FSM in INIT with kill_switch=True stays INIT (not EMERGENCY)."""
        fsm = OrchestratorFSM(config=FsmConfig())
        assert fsm.state == SystemState.INIT

        event = fsm.tick(_fsm_inputs(kill_switch=True))
        # _check_emergency skipped (state==INIT), _eval_init blocks (kill_switch)
        assert event is None
        assert fsm.state == SystemState.INIT


class TestRecoveryBlockedByKillSwitch:
    """EMERGENCY + flat position + kill_switch active -> stays EMERGENCY.

    Source: fsm_orchestrator.py:377 — "not inp.kill_switch_active" required for recovery.
    This is a safety invariant: kill switch MUST prevent recovery even if positions flat.
    """

    def test_recovery_blocked_when_killswitch_active_even_if_flat(self) -> None:
        """position_notional_usd=0.0 + kill_switch=True -> stays EMERGENCY."""
        fsm = OrchestratorFSM(
            state=SystemState.EMERGENCY,
            state_enter_ts=0,
            config=FsmConfig(),
        )

        # Flat position (0.0 < 10.0 threshold), but kill switch still active
        event = fsm.tick(
            _fsm_inputs(
                kill_switch=True,
                position_notional_usd=0.0,
                drawdown_pct=0.0,
            )
        )

        # Must stay EMERGENCY — kill switch blocks recovery
        assert event is None
        assert fsm.state == SystemState.EMERGENCY
