"""Unit tests for LiveEngineV0.

Tests cover:
A) Safety / arming (4 tests):
   - armed=False → 0 calls
   - armed=True + mode=READ_ONLY → 0 calls
   - kill_switch_active=True → place/replace blocked, cancel allowed
   - symbol not in whitelist → blocked

B) Drawdown guard (3 tests):
   - NORMAL → INCREASE_RISK allowed
   - DRAWDOWN → INCREASE_RISK blocked
   - DRAWDOWN → REDUCE_RISK allowed (cancel)

C) Idempotency + retry interaction (3 tests):
   - Duplicate place → 1 underlying call, second cached
   - Retryable failure then success → 1 side-effect
   - Non-retryable error → no retries

D) Circuit breaker (2 tests):
   - Trip breaker → subsequent call rejected
   - Half-open probe success closes breaker

See ADR-036 for design decisions.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grinder.account.contracts import AccountSnapshot, OpenOrderSnap, PositionSnap
from grinder.connectors.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from grinder.connectors.errors import (
    ConnectorNonRetryableError,
    ConnectorTransientError,
)
from grinder.connectors.live_connector import SafeMode
from grinder.connectors.retries import RetryPolicy
from grinder.contracts import Snapshot
from grinder.core import OrderSide, SystemState
from grinder.execution.idempotent_port import IdempotentExchangePort
from grinder.execution.port import NoOpExchangePort
from grinder.execution.types import ActionType, ExecutionAction
from grinder.features.engine import FeatureEngine, FeatureEngineConfig
from grinder.live import (
    BlockReason,
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineV0,
    classify_intent,
)
from grinder.live.cycle_layer import LiveCycleConfig, LiveCycleLayerV1
from grinder.live.fsm_driver import FsmDriver
from grinder.live.fsm_metrics import get_fsm_metrics, reset_fsm_metrics
from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorFSM
from grinder.live.grid_planner import LiveGridConfig, LiveGridPlannerV1
from grinder.live.live_metrics import get_live_engine_metrics, reset_live_engine_metrics
from grinder.risk.drawdown_guard_v1 import (
    DrawdownGuardV1,
    DrawdownGuardV1Config,
    GuardState,
)
from grinder.risk.drawdown_guard_v1 import (
    OrderIntent as RiskIntent,
)

# --- Fixtures ---


@pytest.fixture
def mock_paper_engine() -> MagicMock:
    """Create a mock PaperEngine that returns configurable actions."""
    engine = MagicMock()
    # Default: no actions
    engine.process_snapshot.return_value = MagicMock(actions=[])
    return engine


@pytest.fixture
def noop_port() -> NoOpExchangePort:
    """Create a NoOpExchangePort for testing."""
    return NoOpExchangePort()


@pytest.fixture
def tracking_port() -> MagicMock:
    """Create a mock port that tracks calls."""
    port = MagicMock()
    port.calls = []

    def track_place(**kwargs: Any) -> str:
        port.calls.append(("place_order", kwargs))
        return f"ORDER_{len(port.calls)}"

    def track_cancel(order_id: str) -> bool:
        port.calls.append(("cancel_order", {"order_id": order_id}))
        return True

    def track_replace(**kwargs: Any) -> str:
        port.calls.append(("replace_order", kwargs))
        return f"ORDER_{len(port.calls)}"

    port.place_order.side_effect = track_place
    port.cancel_order.side_effect = track_cancel
    port.replace_order.side_effect = track_replace
    return port


@pytest.fixture
def sample_snapshot() -> Snapshot:
    """Create a sample snapshot for testing."""
    return Snapshot(
        ts=1000000,
        symbol="BTCUSDT",
        bid_price=Decimal("50000"),
        ask_price=Decimal("50001"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal("50000.5"),
        last_qty=Decimal("0.5"),
    )


@pytest.fixture
def place_action() -> ExecutionAction:
    """Create a PLACE action for testing."""
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("49000"),
        quantity=Decimal("0.01"),
        level_id=1,
        reason="GRID_ENTRY",
    )


@pytest.fixture
def cancel_action() -> ExecutionAction:
    """Create a CANCEL action for testing."""
    return ExecutionAction(
        action_type=ActionType.CANCEL,
        order_id="ORDER_123",
        symbol="BTCUSDT",
        reason="GRID_EXIT",
    )


@pytest.fixture
def replace_action() -> ExecutionAction:
    """Create a REPLACE action for testing."""
    return ExecutionAction(
        action_type=ActionType.REPLACE,
        order_id="ORDER_123",
        symbol="BTCUSDT",
        price=Decimal("49500"),
        quantity=Decimal("0.02"),
        level_id=2,
        reason="GRID_ADJUST",
    )


# --- A) Safety / Arming Tests (4 tests) ---


class TestArmingSafety:
    """Tests for arming and mode safety gates."""

    def test_armed_false_blocks_all_writes(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """armed=False → place/cancel/replace NOT called (0 calls)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        config = LiveEngineConfig(armed=False, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config)

        output = engine.process_snapshot(sample_snapshot)

        # Action was blocked
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.NOT_ARMED

        # CRITICAL: 0 port calls
        assert len(tracking_port.calls) == 0

    def test_armed_true_mode_read_only_blocks_writes(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """armed=True + mode=READ_ONLY → blocked with reason code (0 calls)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        config = LiveEngineConfig(armed=True, mode=SafeMode.READ_ONLY)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config)

        output = engine.process_snapshot(sample_snapshot)

        # Action was blocked
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.MODE_NOT_LIVE_TRADE

        # CRITICAL: 0 port calls
        assert len(tracking_port.calls) == 0

    def test_kill_switch_blocks_place_allows_cancel(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
        cancel_action: ExecutionAction,
    ) -> None:
        """kill_switch_active=True → place/replace blocked (0 calls), cancel allowed (1 call)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(
            actions=[place_action, cancel_action]
        )

        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            kill_switch_active=True,
        )
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config)

        output = engine.process_snapshot(sample_snapshot)

        # PLACE blocked
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.KILL_SWITCH_ACTIVE

        # CANCEL allowed
        assert output.live_actions[1].status == LiveActionStatus.EXECUTED
        assert output.live_actions[1].order_id == "ORDER_123"

        # Only 1 call (cancel)
        assert len(tracking_port.calls) == 1
        assert tracking_port.calls[0][0] == "cancel_order"

    def test_symbol_not_in_whitelist_blocked(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """Symbol not in whitelist → blocked before port (0 calls)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            symbol_whitelist=["ETHUSDT"],  # BTCUSDT not in whitelist
        )
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config)

        output = engine.process_snapshot(sample_snapshot)

        # Action was blocked
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.SYMBOL_NOT_WHITELISTED

        # CRITICAL: 0 port calls
        assert len(tracking_port.calls) == 0


# --- B) Drawdown Guard Tests (3 tests) ---


class TestDrawdownGuard:
    """Tests for DrawdownGuardV1 integration."""

    def test_normal_state_allows_increase_risk(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """In NORMAL → INCREASE_RISK allowed → underlying called."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        guard = DrawdownGuardV1()
        # Guard starts in NORMAL state
        assert guard.state == GuardState.NORMAL

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, drawdown_guard=guard)

        output = engine.process_snapshot(sample_snapshot)

        # Action was executed
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED

        # Port was called
        assert len(tracking_port.calls) == 1
        assert tracking_port.calls[0][0] == "place_order"

    def test_drawdown_state_blocks_increase_risk(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """In DRAWDOWN → INCREASE_RISK blocked → 0 calls + decision reason."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        guard = DrawdownGuardV1(config=DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.10")))
        # Force drawdown state by updating with a loss
        guard.update(
            equity_current=Decimal("80000"),  # 20% down from 100k
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        assert guard.state == GuardState.DRAWDOWN

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, drawdown_guard=guard)

        output = engine.process_snapshot(sample_snapshot)

        # Action was blocked
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.DRAWDOWN_BLOCKED
        assert output.live_actions[0].intent == RiskIntent.INCREASE_RISK

        # CRITICAL: 0 port calls
        assert len(tracking_port.calls) == 0

    def test_drawdown_state_allows_reduce_risk_cancel(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        cancel_action: ExecutionAction,
    ) -> None:
        """In DRAWDOWN → REDUCE_RISK/CANCEL allowed → call executed."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[cancel_action])

        guard = DrawdownGuardV1(config=DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.10")))
        # Force drawdown state
        guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        assert guard.state == GuardState.DRAWDOWN

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, drawdown_guard=guard)

        output = engine.process_snapshot(sample_snapshot)

        # CANCEL was executed (always allowed)
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED
        assert output.live_actions[0].intent == RiskIntent.CANCEL

        # Port was called
        assert len(tracking_port.calls) == 1


# --- C) Idempotency + Retry Tests (3 tests) ---


class TestIdempotencyRetry:
    """Tests for idempotency and retry interaction."""

    def test_duplicate_place_returns_cached(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """Duplicate place (same params) → 1 underlying call, second returns cached."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        # Wrap with IdempotentExchangePort
        idempotent_port = IdempotentExchangePort(inner=noop_port)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, idempotent_port, config)

        # First call
        output1 = engine.process_snapshot(sample_snapshot)
        assert output1.live_actions[0].status == LiveActionStatus.EXECUTED
        first_order_id = output1.live_actions[0].order_id

        # Second call with same params (same snapshot)
        output2 = engine.process_snapshot(sample_snapshot)
        assert output2.live_actions[0].status == LiveActionStatus.EXECUTED
        second_order_id = output2.live_actions[0].order_id

        # Same order ID (cached)
        assert first_order_id == second_order_id

        # IdempotentPort stats show cache hit
        stats = idempotent_port.stats
        assert stats.place_cached >= 1

    def test_retryable_failure_then_success(
        self,
        mock_paper_engine: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """Retryable transient failure then success → still 1 side-effect (idempotency key stable)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        # Create a port that fails once then succeeds
        failing_port = MagicMock()
        call_count = [0]

        def place_with_retry(**kwargs: Any) -> str:
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectorTransientError("Network error")
            return "ORDER_SUCCESS"

        failing_port.place_order.side_effect = place_with_retry

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        # Fast retries for testing
        retry_policy = RetryPolicy(max_attempts=3, base_delay_ms=1)
        engine = LiveEngineV0(mock_paper_engine, failing_port, config, retry_policy=retry_policy)

        output = engine.process_snapshot(sample_snapshot)

        # Succeeded after retry
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED
        assert output.live_actions[0].order_id == "ORDER_SUCCESS"
        assert output.live_actions[0].attempts == 2  # First failed, second succeeded

        # Only 2 actual calls (1 fail + 1 success)
        assert call_count[0] == 2

    def test_non_retryable_error_no_retries(
        self,
        mock_paper_engine: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """Non-retryable error (e.g., 400 mapped) → no retries (attempts=1)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        # Create a port that returns non-retryable error
        failing_port = MagicMock()
        call_count = [0]

        def place_non_retryable(**kwargs: Any) -> str:
            call_count[0] += 1
            raise ConnectorNonRetryableError("Invalid symbol")

        failing_port.place_order.side_effect = place_non_retryable

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        retry_policy = RetryPolicy(max_attempts=3, base_delay_ms=1)
        engine = LiveEngineV0(mock_paper_engine, failing_port, config, retry_policy=retry_policy)

        output = engine.process_snapshot(sample_snapshot)

        # Failed immediately
        assert output.live_actions[0].status == LiveActionStatus.FAILED
        assert output.live_actions[0].block_reason == BlockReason.NON_RETRYABLE_ERROR
        assert output.live_actions[0].attempts == 1  # No retries

        # Only 1 call (no retries for non-retryable)
        assert call_count[0] == 1


# --- D) Circuit Breaker Tests (2 tests) ---


class TestCircuitBreaker:
    """Tests for circuit breaker integration."""

    def test_tripped_breaker_rejects_call(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """Trip breaker → subsequent call rejected before underlying."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        # Create breaker that trips after 1 failure
        breaker = CircuitBreaker(
            config=CircuitBreakerConfig(
                failure_threshold=1,
                open_interval_s=60.0,  # Long cooldown
            )
        )

        # Wrap with IdempotentExchangePort + breaker
        idempotent_port = IdempotentExchangePort(
            inner=noop_port,
            breaker=breaker,
            trip_on=lambda _: True,  # Trip on any exception
        )

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, idempotent_port, config)

        # Force breaker to OPEN by recording failure
        breaker.record_failure("place")

        # Now try to execute
        output = engine.process_snapshot(sample_snapshot)

        # Should fail due to circuit breaker
        assert output.live_actions[0].status == LiveActionStatus.FAILED
        assert output.live_actions[0].block_reason == BlockReason.CIRCUIT_BREAKER_OPEN
        assert "OPEN" in str(output.live_actions[0].error)

    def test_half_open_probe_success_closes_breaker(self) -> None:
        """Half-open probe success closes breaker (bounded-time via fake clock)."""

        # Create breaker with short cooldown
        class FakeClock:
            def __init__(self) -> None:
                self._now = 0.0

            def time(self) -> float:
                return self._now

            def advance(self, seconds: float) -> None:
                self._now += seconds

        fake_clock = FakeClock()
        breaker = CircuitBreaker(
            config=CircuitBreakerConfig(
                failure_threshold=1,
                open_interval_s=1.0,  # 1 second cooldown
                half_open_probe_count=1,
                success_threshold=1,
            ),
            clock=fake_clock,
        )

        # Trip the breaker
        breaker.record_failure("place")
        assert breaker.state("place") == CircuitState.OPEN

        # Advance clock past cooldown
        fake_clock.advance(2.0)

        # Now in HALF_OPEN
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # Successful probe closes breaker
        breaker.record_success("place")
        assert breaker.state("place") == CircuitState.CLOSED


# --- Intent Classification Tests ---


class TestIntentClassification:
    """Tests for classify_intent function."""

    def test_cancel_classified_as_cancel(self, cancel_action: ExecutionAction) -> None:
        """CANCEL action → CANCEL intent."""
        assert classify_intent(cancel_action) == RiskIntent.CANCEL

    def test_place_classified_as_increase_risk(self, place_action: ExecutionAction) -> None:
        """PLACE action → INCREASE_RISK intent."""
        assert classify_intent(place_action) == RiskIntent.INCREASE_RISK

    def test_replace_classified_as_increase_risk(self, replace_action: ExecutionAction) -> None:
        """REPLACE action → INCREASE_RISK intent."""
        assert classify_intent(replace_action) == RiskIntent.INCREASE_RISK

    def test_noop_classified_as_cancel(self) -> None:
        """NOOP action → CANCEL intent (safe)."""
        noop_action = ExecutionAction(action_type=ActionType.NOOP)
        assert classify_intent(noop_action) == RiskIntent.CANCEL


# --- E) FSM State Gate Tests (Launch-13) ---


class TestFsmStateGate:
    """Tests for Gate 7: FSM state-based intent blocking."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_fsm_paused_blocks_increase_risk(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """FSM in PAUSED → INCREASE_RISK blocked with FSM_STATE_BLOCKED."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        # state_enter_ts matches sample_snapshot.ts so cooldown hasn't elapsed
        # (prevents PAUSED→ACTIVE recovery during tick)
        fsm = OrchestratorFSM(state=SystemState.PAUSED, state_enter_ts=1000000, config=FsmConfig())
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        output = engine.process_snapshot(sample_snapshot)

        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.FSM_STATE_BLOCKED
        assert output.live_actions[0].intent == RiskIntent.INCREASE_RISK

        # CRITICAL: 0 port calls
        assert len(tracking_port.calls) == 0

    def test_fsm_paused_allows_cancel(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        cancel_action: ExecutionAction,
    ) -> None:
        """FSM in PAUSED → CANCEL allowed (reduce-risk intents pass Gate 7)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[cancel_action])

        # state_enter_ts matches sample_snapshot.ts so cooldown hasn't elapsed
        fsm = OrchestratorFSM(state=SystemState.PAUSED, state_enter_ts=1000000, config=FsmConfig())
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        output = engine.process_snapshot(sample_snapshot)

        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED
        assert output.live_actions[0].intent == RiskIntent.CANCEL

        # Port was called
        assert len(tracking_port.calls) == 1


# --- F) FSM Loop Wiring Tests (Launch-13 PR3) ---


class TestFsmLoopWiring:
    """Tests for FSM tick wiring in process_snapshot.

    PR3 proves: FsmDriver.step() is called from the real loop, driven by
    runtime signals (kill switch, drawdown, operator override env var),
    and the engine write-path blocks appropriately when state changes.
    """

    def setup_method(self) -> None:
        reset_fsm_metrics()

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure GRINDER_OPERATOR_OVERRIDE is unset for every test."""
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

    def test_kill_switch_triggers_emergency_transition(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
    ) -> None:
        """kill_switch_active=True → FSM ACTIVE→EMERGENCY transition."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            kill_switch_active=True,
        )
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snapshot)

        assert driver.state == SystemState.EMERGENCY
        metrics = get_fsm_metrics()
        assert ("ACTIVE", "EMERGENCY", "KILL_SWITCH") in metrics.transitions

    def test_operator_override_pause_blocks_via_gate6(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        place_action: ExecutionAction,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GRINDER_OPERATOR_OVERRIDE=PAUSE → ACTIVE→PAUSED → PLACE blocked by Gate 7.

        This also proves tick-before-gate ordering: the FSM transitions in
        the same snapshot that contains the PLACE action, and Gate 7 sees
        the new state (PAUSED) which blocks INCREASE_RISK.
        """
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])
        monkeypatch.setenv("GRINDER_OPERATOR_OVERRIDE", "PAUSE")

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snapshot)

        # FSM transitioned to PAUSED
        assert driver.state == SystemState.PAUSED
        # PLACE blocked by Gate 7 (not Gate 3 — kill switch is off)
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.FSM_STATE_BLOCKED
        # 0 port calls
        assert len(tracking_port.calls) == 0

    def test_invalid_operator_override_warns_and_still_ticks(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid GRINDER_OPERATOR_OVERRIDE → warning + tick still runs."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])
        monkeypatch.setenv("GRINDER_OPERATOR_OVERRIDE", "INVALID_VALUE")

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            engine.process_snapshot(snapshot)

        # Warning was logged
        assert any("GRINDER_OPERATOR_OVERRIDE" in r.message for r in caplog.records)
        # FSM still ticked (state gauge updated, stays ACTIVE since override=None)
        assert driver.state == SystemState.ACTIVE
        metrics = get_fsm_metrics()
        assert metrics._current_state == SystemState.ACTIVE

    def test_drawdown_breached_triggers_emergency(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
    ) -> None:
        """DrawdownGuardV1 in DRAWDOWN → drawdown_breached=True → FSM ACTIVE→EMERGENCY."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])

        guard = DrawdownGuardV1(
            config=DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.10")),
        )
        guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        assert guard.is_drawdown

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            drawdown_guard=guard,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snapshot)

        assert driver.state == SystemState.EMERGENCY
        metrics = get_fsm_metrics()
        assert ("ACTIVE", "EMERGENCY", "DD_BREACH") in metrics.transitions

    def test_safe_defaults_keep_fsm_active(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """No active signals → FSM stays ACTIVE → PLACE passes Gate 7."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        output = engine.process_snapshot(sample_snapshot)

        assert driver.state == SystemState.ACTIVE
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED
        assert len(tracking_port.calls) == 1

    def test_snapshot_ts_used_for_fsm_clock(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
    ) -> None:
        """Duration gauge uses snapshot.ts (not wall clock) for determinism."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=5000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snapshot)

        metrics = get_fsm_metrics()
        # duration = (5000 - 1000) / 1000.0 = 4.0s
        # Would be wildly different if time.time() were used
        assert metrics.state_duration_s == pytest.approx(4.0)

    def test_override_normalizes_whitespace_and_case_pause(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        place_action: ExecutionAction,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """' pause ' (whitespace + lowercase) → normalized to PAUSE → Gate 7 blocks."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])
        monkeypatch.setenv("GRINDER_OPERATOR_OVERRIDE", " pause ")

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snapshot)

        assert driver.state == SystemState.PAUSED
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED
        assert output.live_actions[0].block_reason == BlockReason.FSM_STATE_BLOCKED

    def test_override_normalizes_whitespace_and_case_emergency(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        place_action: ExecutionAction,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """' emergency ' (whitespace + lowercase) → normalized to EMERGENCY → blocks."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])
        monkeypatch.setenv("GRINDER_OPERATOR_OVERRIDE", " emergency ")

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snapshot)

        assert driver.state == SystemState.EMERGENCY
        assert output.live_actions[0].status == LiveActionStatus.BLOCKED

    def test_override_whitespace_only_treated_as_unset(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        place_action: ExecutionAction,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """'   ' (whitespace only) → treated as unset, no warning, FSM stays ACTIVE."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])
        monkeypatch.setenv("GRINDER_OPERATOR_OVERRIDE", "   ")

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
        )

        snapshot = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )

        with caplog.at_level(logging.WARNING, logger="grinder.live.engine"):
            output = engine.process_snapshot(snapshot)

        # No warning (whitespace = unset, not invalid)
        assert not any("GRINDER_OPERATOR_OVERRIDE" in r.message for r in caplog.records)
        # FSM stays ACTIVE, PLACE goes through
        assert driver.state == SystemState.ACTIVE
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED
        assert len(tracking_port.calls) == 1


# --- G) FSM Ghost Orders Prevention (PR-338) ---


class TestFsmDeferPaperEngine:
    """Tests for PR-338: defer PaperEngine during FSM INIT/READY.

    Bug: paper engine mutates internal state (via NoOp port) before FSM
    reaches ACTIVE. Ghost orders freeze reconciliation after ACTIVE transition.
    Fix: skip paper engine evaluation when FSM state is INIT or READY.
    """

    def setup_method(self) -> None:
        reset_fsm_metrics()

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

    def test_init_state_skips_paper_engine(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
    ) -> None:
        """FSM INIT → paper engine NOT called, 0 actions, 0 port calls."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])

        fsm = OrchestratorFSM(state=SystemState.INIT, state_enter_ts=0)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        output = engine.process_snapshot(sample_snapshot)

        # Paper engine must NOT be called
        mock_paper_engine.process_snapshot.assert_not_called()
        # 0 live actions
        assert output.live_actions == []
        # 0 port calls
        assert len(tracking_port.calls) == 0
        # FSM still ticked (advances toward READY)
        assert driver.state == SystemState.READY

    def test_ready_state_skips_paper_engine(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
    ) -> None:
        """FSM READY → paper engine NOT called, 0 actions."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])

        fsm = OrchestratorFSM(state=SystemState.READY, state_enter_ts=0)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        snapshot = Snapshot(
            ts=100_000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snapshot)

        mock_paper_engine.process_snapshot.assert_not_called()
        assert output.live_actions == []
        assert len(tracking_port.calls) == 0

    def test_active_state_runs_paper_engine(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """FSM ACTIVE → paper engine called, actions processed normally."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        output = engine.process_snapshot(sample_snapshot)

        mock_paper_engine.process_snapshot.assert_called_once()
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED
        assert len(tracking_port.calls) == 1

    def test_init_to_active_lifecycle(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        place_action: ExecutionAction,
    ) -> None:
        """Full INIT→READY→ACTIVE lifecycle: paper engine runs only after ACTIVE.

        AC1 repro: ghost orders cannot form because paper engine is deferred
        during INIT and READY states.
        """
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        fsm = OrchestratorFSM(state=SystemState.INIT, state_enter_ts=0)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        def make_snapshot(ts: int) -> Snapshot:
            return Snapshot(
                ts=ts,
                symbol="BTCUSDT",
                bid_price=Decimal("50000"),
                ask_price=Decimal("50001"),
                bid_qty=Decimal("1"),
                ask_qty=Decimal("1"),
                last_price=Decimal("50000.5"),
                last_qty=Decimal("0.5"),
            )

        # Tick 1: INIT → paper deferred, FSM advances to READY
        out1 = engine.process_snapshot(make_snapshot(1000))
        state_after_1: SystemState = driver.state
        assert state_after_1 == SystemState.READY
        mock_paper_engine.process_snapshot.assert_not_called()
        assert out1.live_actions == []

        # Tick 2: READY → paper deferred, FSM advances to ACTIVE
        out2 = engine.process_snapshot(make_snapshot(2000))
        state_after_2: SystemState = driver.state
        assert state_after_2 == SystemState.ACTIVE
        mock_paper_engine.process_snapshot.assert_not_called()
        assert out2.live_actions == []

        # Tick 3: ACTIVE → paper engine runs, actions reach port
        out3 = engine.process_snapshot(make_snapshot(3000))
        state_after_3: SystemState = driver.state
        assert state_after_3 == SystemState.ACTIVE
        mock_paper_engine.process_snapshot.assert_called_once()
        assert len(out3.live_actions) == 1
        assert out3.live_actions[0].status == LiveActionStatus.EXECUTED
        assert len(tracking_port.calls) == 1

    def test_no_fsm_driver_runs_paper_engine(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        place_action: ExecutionAction,
    ) -> None:
        """AC2: FSM disabled (fsm_driver=None) → paper engine runs immediately."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[place_action])

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=None)

        output = engine.process_snapshot(sample_snapshot)

        mock_paper_engine.process_snapshot.assert_called_once()
        assert len(output.live_actions) == 1
        assert output.live_actions[0].status == LiveActionStatus.EXECUTED

    def test_deferred_output_has_to_dict(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
    ) -> None:
        """Deferred paper output supports to_dict() for LiveEngineOutput serialization."""
        fsm = OrchestratorFSM(state=SystemState.INIT, state_enter_ts=0)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(mock_paper_engine, tracking_port, config, fsm_driver=driver)

        output = engine.process_snapshot(sample_snapshot)

        # Must not raise
        output_dict = output.to_dict()
        assert output_dict["paper_output"]["actions"] == []
        assert "deferred_by_fsm" not in output_dict["paper_output"]


class TestFeatureEngine:
    """Tests for PR-L0: FeatureEngine wiring in LiveEngineV0.

    FeatureEngine computes NATR/volatility features from snapshots.
    PR-L0 lifts it into LiveEngineV0 for future LiveGridPlanner consumption.
    """

    def setup_method(self) -> None:
        reset_fsm_metrics()

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

    def test_feature_engine_none_unchanged(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
    ) -> None:
        """feature_engine=None (default) → no feature snapshot, paper engine still called."""
        config = LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY)
        engine = LiveEngineV0(mock_paper_engine, noop_port, config, feature_engine=None)

        engine.process_snapshot(sample_snapshot)

        assert engine.last_feature_snapshot is None
        mock_paper_engine.process_snapshot.assert_called_once()

    def test_feature_engine_called_on_snapshot(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
    ) -> None:
        """feature_engine set → process_snapshot called, snapshot stored."""
        feature_engine = FeatureEngine(FeatureEngineConfig())
        config = LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY)
        engine = LiveEngineV0(mock_paper_engine, noop_port, config, feature_engine=feature_engine)

        engine.process_snapshot(sample_snapshot)

        snap = engine.last_feature_snapshot
        assert snap is not None
        assert snap.symbol == "BTCUSDT"
        assert hasattr(snap, "natr_bps")

    def test_feature_engine_runs_during_fsm_defer(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
    ) -> None:
        """FSM INIT → paper engine deferred, but FeatureEngine still runs (bar warmup)."""
        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[])

        feature_engine = FeatureEngine(FeatureEngineConfig())
        fsm = OrchestratorFSM(state=SystemState.INIT, state_enter_ts=0)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
            feature_engine=feature_engine,
        )

        engine.process_snapshot(sample_snapshot)

        # FeatureEngine ran (bar building continues during warmup)
        assert engine.last_feature_snapshot is not None
        assert engine.last_feature_snapshot.symbol == "BTCUSDT"
        # Paper engine was deferred (PR-338 logic)
        mock_paper_engine.process_snapshot.assert_not_called()
        # FSM still ticked (advances INIT → READY)
        assert driver.state == SystemState.READY


# --- I) LiveGridPlanner wiring (PR-L2) ---


class TestLiveGridPlanner:
    """PR-L2: LiveGridPlannerV1 wiring into LiveEngineV0."""

    def setup_method(self) -> None:
        """Reset FSM metrics to avoid inter-test leakage."""
        reset_fsm_metrics()

    def test_planner_disabled_uses_paper_engine(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Planner disabled (default) -> PaperEngine is called."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "0")

        config = LiveEngineConfig(armed=False, mode=SafeMode.READ_ONLY)
        engine = LiveEngineV0(mock_paper_engine, noop_port, config, grid_planners=None)

        engine.process_snapshot(sample_snapshot)

        mock_paper_engine.process_snapshot.assert_called_once()

    def test_planner_enabled_no_snapshot_zero_actions(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Planner enabled but no AccountSnapshot yet -> 0 actions (safe startup)."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            noop_port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

        output = engine.process_snapshot(sample_snapshot)

        # No account snapshot -> 0 actions (safe startup)
        assert output.live_actions == []
        # PaperEngine NOT called (planner path active)
        mock_paper_engine.process_snapshot.assert_not_called()

    def test_planner_enabled_with_snapshot_produces_actions(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Planner enabled + AccountSnapshot present -> PLACE actions for missing levels."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            noop_port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

        # Simulate AccountSync having run: empty exchange (no orders)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000, source="test"
        )

        output = engine.process_snapshot(sample_snapshot)

        # Planner sees empty exchange -> emits PLACE for all desired levels (2 buy + 2 sell = 4)
        assert len(output.live_actions) == 4
        for la in output.live_actions:
            assert la.action.action_type == ActionType.PLACE
        # PaperEngine NOT called
        mock_paper_engine.process_snapshot.assert_not_called()

    def test_planner_fsm_defer_skips_planner(
        self,
        mock_paper_engine: MagicMock,
        tracking_port: MagicMock,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FSM INIT -> planner skipped (deferred), FeatureEngine still runs."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        feature_engine = FeatureEngine(FeatureEngineConfig())
        fsm = OrchestratorFSM(state=SystemState.INIT, state_enter_ts=0)
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            tracking_port,
            config,
            fsm_driver=driver,
            feature_engine=feature_engine,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

        # Even with a snapshot, FSM defer should prevent planner from running
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000, source="test"
        )

        output = engine.process_snapshot(sample_snapshot)

        # Deferred: no actions
        assert output.live_actions == []
        # FeatureEngine still ran (bar warmup continues)
        assert engine.last_feature_snapshot is not None
        # PaperEngine also deferred
        mock_paper_engine.process_snapshot.assert_not_called()
        # FSM advanced (INIT -> READY)
        assert driver.state == SystemState.READY


# --- PR-INV-2: suppress_increase wiring ---


class TestSuppressIncreaseWiring:
    """PR-INV-2: FSM non-ACTIVE triggers cancel-only planner mode."""

    def setup_method(self) -> None:
        reset_fsm_metrics()

    def test_paused_suppresses_place_actions(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FSM PAUSED + planner enabled -> no PLACE actions (cancel-only)."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        fsm = OrchestratorFSM(state=SystemState.PAUSED, state_enter_ts=1000, config=FsmConfig())
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            noop_port,
            config,
            fsm_driver=driver,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

        # Simulate empty exchange (all levels missing -> normally 4 PLACE)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000, source="test"
        )

        output = engine.process_snapshot(sample_snapshot)

        # PAUSED -> suppress_increase=True -> zero PLACE actions
        place_actions = [
            la for la in output.live_actions if la.action.action_type == ActionType.PLACE
        ]
        assert len(place_actions) == 0

    def test_active_allows_place_actions(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FSM ACTIVE + planner enabled -> PLACE actions pass through."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        fsm = OrchestratorFSM(state=SystemState.ACTIVE, state_enter_ts=1000, config=FsmConfig())
        driver = FsmDriver(fsm)

        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            noop_port,
            config,
            fsm_driver=driver,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

        # Empty exchange -> 4 PLACE (2 buy + 2 sell)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000, source="test"
        )

        output = engine.process_snapshot(sample_snapshot)

        place_actions = [
            la for la in output.live_actions if la.action.action_type == ActionType.PLACE
        ]
        assert len(place_actions) == 4


# --- PR-INV-1: Position-aware intent + inventory cap gate ---


class TestPositionAwareIntent:
    """Tests for position-aware classify_intent (PR-INV-1)."""

    def test_long_sell_is_reduce_risk(self) -> None:
        """LONG position + SELL PLACE → REDUCE_RISK."""
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
        )
        intent = classify_intent(action, pos_sign=1)
        assert intent == RiskIntent.REDUCE_RISK

    def test_long_buy_is_increase_risk(self) -> None:
        """LONG position + BUY PLACE → INCREASE_RISK."""
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
        )
        intent = classify_intent(action, pos_sign=1)
        assert intent == RiskIntent.INCREASE_RISK

    def test_short_buy_is_reduce_risk(self) -> None:
        """SHORT position + BUY PLACE → REDUCE_RISK."""
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
        )
        intent = classify_intent(action, pos_sign=-1)
        assert intent == RiskIntent.REDUCE_RISK

    def test_short_sell_is_increase_risk(self) -> None:
        """SHORT position + SELL PLACE → INCREASE_RISK."""
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
        )
        intent = classify_intent(action, pos_sign=-1)
        assert intent == RiskIntent.INCREASE_RISK

    def test_unknown_pos_sign_is_increase_risk(self) -> None:
        """pos_sign=None (BOTH/unknown) + PLACE → INCREASE_RISK (fail-closed)."""
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
        )
        intent = classify_intent(action, pos_sign=None)
        assert intent == RiskIntent.INCREASE_RISK


class TestMaxPositionGate:
    """Tests for Gate 5: max position cap (PR-INV-1)."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        max_position_usd: float | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        config = LiveEngineConfig(
            armed=True,
            mode=SafeMode.LIVE_TRADE,
            max_position_usd=max_position_usd,
        )
        mock_syncer = MagicMock()
        return LiveEngineV0(
            paper_engine=mock_paper_engine,
            exchange_port=port,
            config=config,
            account_syncer=mock_syncer,
        )

    def test_increase_risk_blocked_above_cap(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        place_action: ExecutionAction,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """position_notional >= cap + INCREASE_RISK → BLOCKED."""
        engine = self._make_engine(mock_paper_engine, noop_port, 1000.0, monkeypatch)
        engine._position_notional_usd = 1500.0
        result = engine._process_action(place_action, ts=1000)
        assert result.status == LiveActionStatus.BLOCKED
        assert result.block_reason == BlockReason.MAX_POSITION_EXCEEDED

    def test_cancel_allowed_above_cap(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        cancel_action: ExecutionAction,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """position_notional >= cap + CANCEL → ALLOWED (not blocked by gate)."""
        engine = self._make_engine(mock_paper_engine, noop_port, 1000.0, monkeypatch)
        engine._position_notional_usd = 1500.0
        result = engine._process_action(cancel_action, ts=1000)
        # CANCEL should pass through Gate 5 (intent != INCREASE_RISK)
        assert result.block_reason != BlockReason.MAX_POSITION_EXCEEDED

    def test_reduce_risk_allowed_above_cap(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """position_notional >= cap + REDUCE_RISK SELL (LONG pos) → ALLOWED."""
        engine = self._make_engine(mock_paper_engine, noop_port, 1000.0, monkeypatch)
        engine._position_notional_usd = 1500.0
        # Set up a LONG position so SELL = REDUCE_RISK
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.03"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )
        sell_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        result = engine._process_action(sell_action, ts=1000)
        # SELL when LONG = REDUCE_RISK, should pass Gate 5
        assert result.block_reason != BlockReason.MAX_POSITION_EXCEEDED


# =============================================================================
# PR-INV-3: Cycle layer integration tests
# =============================================================================


class TestCycleLayerIntegration:
    """Integration tests for LiveCycleLayerV1 wired into LiveEngineV0."""

    def test_cycle_layer_generates_tp_on_fill(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When a grid order disappears (fill), cycle layer generates TP PLACE."""
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        cycle_layer = LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(
            mock_paper_engine,
            noop_port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer,
        )

        # Snapshot 1: one BUY grid order present
        grid_order = OpenOrderSnap(
            order_id="grinder_d_BTCUSDT_3_1000_1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1000000,
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(grid_order,),
            ts=1000000,
            source="test",
        )
        engine.process_snapshot(sample_snapshot)

        # Snapshot 2: grid order gone (filled) -> cycle layer should generate TP
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(),
            ts=2000000,
            source="test",
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snap2)

        # Should have TP action(s) among live_actions
        tp_actions = [la for la in output.live_actions if la.action.reason == "TP_CLOSE"]
        assert len(tp_actions) >= 1
        tp = tp_actions[0]
        assert tp.action.reduce_only is True
        assert tp.action.client_order_id is not None
        assert tp.action.client_order_id.startswith("grinder_tp_")
        assert tp.action.side == OrderSide.SELL  # opposite of BUY fill

    def test_tp_classified_as_reduce_risk(self) -> None:
        """TP SELL when LONG (pos_sign=+1) classifies as REDUCE_RISK."""
        tp_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50050"),
            quantity=Decimal("0.01"),
            reason="TP_CLOSE",
            reduce_only=True,
            client_order_id="grinder_tp_BTCUSDT_3_1000_1",
        )
        # LONG position + SELL -> REDUCE_RISK
        intent = classify_intent(tp_action, pos_sign=1)
        assert intent == RiskIntent.REDUCE_RISK

        # SHORT position + BUY TP -> REDUCE_RISK
        tp_buy = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49950"),
            quantity=Decimal("0.01"),
            reason="TP_CLOSE",
            reduce_only=True,
        )
        intent = classify_intent(tp_buy, pos_sign=-1)
        assert intent == RiskIntent.REDUCE_RISK


class TestGridFreezeInPosition:
    """Tests for grid freeze when position is open (pos != 0).

    When GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION=1 and the symbol
    has a non-zero position, the planner is skipped (no GRID_SHIFT)
    and replenish actions are filtered out. TP reduce-only actions
    from the cycle layer are still allowed.
    """

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
        *,
        freeze_enabled: bool = True,
        cycle_enabled: bool = True,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        if freeze_enabled:
            monkeypatch.setenv("GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION", "1")
        if cycle_enabled:
            monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        cycle_layer = LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer if cycle_enabled else None,
        )

    @staticmethod
    def _position_snapshot(qty: str = "0.01") -> AccountSnapshot:
        """AccountSnapshot with a LONG position of given qty."""
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal(qty),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_3_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49990"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )

    @staticmethod
    def _empty_position_snapshot() -> AccountSnapshot:
        """AccountSnapshot with zero position (cycle closed)."""
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="BOTH",
                    qty=Decimal("0"),
                    entry_price=Decimal("0"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=3000000,
                ),
            ),
            open_orders=(),
            ts=3000000,
            source="test",
        )

    def test_position_open_no_grid_shift(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos != 0 → planner skipped, no CANCEL/PLACE grid actions."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = self._position_snapshot()

        output = engine.process_snapshot(sample_snapshot)

        # No planner-generated actions (all GRID_SHIFT cancelled)
        grid_actions = [
            la for la in output.live_actions if la.action.reason not in ("TP_CLOSE", "TP_EXPIRY")
        ]
        assert grid_actions == [], f"Expected no grid actions, got {grid_actions}"

    def test_position_open_no_replenish(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos != 0 → replenish actions filtered out."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)

        # Snap 1: grid order present (seed cycle layer)
        grid_order = OpenOrderSnap(
            order_id="grinder_d_BTCUSDT_3_1000_1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("49990"),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1000000,
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0"),
                    entry_price=Decimal("0"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000000,
                ),
            ),
            open_orders=(grid_order,),
            ts=1000000,
            source="test",
        )
        engine.process_snapshot(sample_snapshot)

        # Snap 2: order filled → position opened → freeze active
        monkeypatch.setenv("GRINDER_LIVE_REPLENISH_ENABLED", "1")
        monkeypatch.setenv("GRINDER_REPLENISH_MAX_LEVELS", "5")
        engine._last_account_snapshot = self._position_snapshot(qty="0.01")
        # Remove the grid order from open_orders to simulate fill
        engine._last_account_snapshot = AccountSnapshot(
            positions=self._position_snapshot().positions,
            open_orders=(),
            ts=2000000,
            source="test",
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snap2)

        replenish = [la for la in output.live_actions if la.action.reason == "REPLENISH"]
        assert replenish == [], f"Expected no replenish, got {replenish}"

    def test_position_open_tp_still_allowed(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos != 0 → TP reduce-only actions still generated by cycle layer."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)

        # Snap 1: grid order present (seed cycle layer with known order)
        grid_order = OpenOrderSnap(
            order_id="grinder_d_BTCUSDT_3_1000_1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            qty=Decimal("0.01"),
            filled_qty=Decimal("0"),
            reduce_only=False,
            status="NEW",
            ts=1000000,
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(grid_order,),
            ts=1000000,
            source="test",
        )
        engine.process_snapshot(sample_snapshot)

        # Snap 2: order gone (filled) + position open → freeze active + TP generated
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=2000000,
                ),
            ),
            open_orders=(),
            ts=2000000,
            source="test",
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snap2)

        tp_actions = [la for la in output.live_actions if la.action.reason == "TP_CLOSE"]
        assert len(tp_actions) >= 1, "TP reduce-only should pass through freeze"
        assert tp_actions[0].action.reduce_only is True

    def test_position_zero_planner_resumes(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos → 0 → planner resumes normal grid actions."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)

        # With position: frozen
        engine._last_account_snapshot = self._position_snapshot()
        snap1 = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out1 = engine.process_snapshot(snap1)
        grid1 = [
            la for la in out1.live_actions if la.action.reason not in ("TP_CLOSE", "TP_EXPIRY")
        ]
        assert grid1 == [], "Should be frozen when position open"

        # Position closed: planner should produce grid actions
        engine._last_account_snapshot = self._empty_position_snapshot()
        snap2 = Snapshot(
            ts=3000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out2 = engine.process_snapshot(snap2)
        grid2 = [
            la for la in out2.live_actions if la.action.reason not in ("TP_CLOSE", "TP_EXPIRY")
        ]
        assert len(grid2) > 0, "Planner should resume when position is zero"


class TestGridShiftAntiChurn:
    """Tests for GRID_SHIFT suppression via min-move threshold."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
        *,
        min_move_bps: int = 50,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_GRID_SHIFT_MIN_MOVE_BPS", str(min_move_bps))

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

    def test_small_move_suppresses_grid_shift(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mid moves < threshold → GRID_SHIFT actions suppressed."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch, min_move_bps=50)

        # Initial: place grid at mid=50000
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )
        snap1 = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out1 = engine.process_snapshot(snap1)
        # First tick: grid placed (GRID_FILL), anchor set
        grid_fill_1 = [la for la in out1.live_actions if la.action.reason == "GRID_FILL"]
        assert len(grid_fill_1) > 0, "Should place initial grid"

        # Simulate: orders exist on exchange at old prices, mid shifts by ~10bps (small)
        # 50000 * 10/10000 = 50 — so 50025 is only 5bps move
        old_orders = tuple(
            OpenOrderSnap(
                order_id=f"grinder_d_BTCUSDT_{i}_1000000_{i}",
                symbol="BTCUSDT",
                side="BUY" if i <= 2 else "SELL",
                order_type="LIMIT",
                price=Decimal("49960") if i <= 2 else Decimal("50040"),
                qty=Decimal("0.01"),
                filled_qty=Decimal("0"),
                reduce_only=False,
                status="NEW",
                ts=1000000,
            )
            for i in range(1, 5)
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=old_orders, ts=2000000, source="test"
        )
        # Move mid by 5bps (50000 → 50025): below 50bps threshold
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50025"),
            ask_price=Decimal("50026"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50025.5"),
            last_qty=Decimal("0.5"),
        )
        out2 = engine.process_snapshot(snap2)
        shift_actions = [la for la in out2.live_actions if la.action.reason == "GRID_SHIFT"]
        assert shift_actions == [], "GRID_SHIFT should be suppressed for small move"

    def test_large_move_allows_grid_shift(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mid moves >= threshold → GRID_SHIFT actions pass through."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch, min_move_bps=50)

        # Initial grid placement
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )
        snap1 = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snap1)

        # Orders at old prices, mid shifts by 100bps (50000 → 50500)
        old_orders = tuple(
            OpenOrderSnap(
                order_id=f"grinder_d_BTCUSDT_{i}_1000000_{i}",
                symbol="BTCUSDT",
                side="BUY" if i <= 2 else "SELL",
                order_type="LIMIT",
                price=Decimal("49960") if i <= 2 else Decimal("50040"),
                qty=Decimal("0.01"),
                filled_qty=Decimal("0"),
                reduce_only=False,
                status="NEW",
                ts=1000000,
            )
            for i in range(1, 5)
        )
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=old_orders, ts=2000000, source="test"
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50500"),
            ask_price=Decimal("50501"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50500.5"),
            last_qty=Decimal("0.5"),
        )
        out2 = engine.process_snapshot(snap2)
        shift_actions = [la for la in out2.live_actions if la.action.reason == "GRID_SHIFT"]
        assert len(shift_actions) > 0, "GRID_SHIFT should pass for large move"

    def test_grid_fill_always_allowed(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GRID_FILL (missing orders) always pass through regardless of threshold."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch, min_move_bps=50)

        # Empty exchange: all orders are GRID_FILL (missing)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )
        snap = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out = engine.process_snapshot(snap)
        fill_actions = [la for la in out.live_actions if la.action.reason == "GRID_FILL"]
        assert len(fill_actions) > 0, "GRID_FILL always allowed"


class TestGridUnfreezeAnchorReset:
    """PR-ANTI-CHURN-2: anchor reset when grid unfreezes after position closes."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
        *,
        min_move_bps: int = 50,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION", "1")
        monkeypatch.setenv("GRINDER_LIVE_GRID_SHIFT_MIN_MOVE_BPS", str(min_move_bps))

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

    @staticmethod
    def _snap(mid: str = "50000") -> Snapshot:
        mid_d = Decimal(mid)
        return Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=mid_d,
            ask_price=mid_d + 1,
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=mid_d,
            last_qty=Decimal("0.5"),
        )

    @staticmethod
    def _pos_snapshot(qty: str = "0.01") -> AccountSnapshot:
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal(qty),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000000,
                ),
            ),
            open_orders=(),
            ts=1000000,
            source="test",
        )

    @staticmethod
    def _flat_snapshot_with_orders() -> AccountSnapshot:
        """Flat position + stale grid orders at old prices."""
        old_orders = tuple(
            OpenOrderSnap(
                order_id=f"grinder_d_BTCUSDT_{i}_1000000_{i}",
                symbol="BTCUSDT",
                side="BUY" if i <= 2 else "SELL",
                order_type="LIMIT",
                price=Decimal("49960") if i <= 2 else Decimal("50040"),
                qty=Decimal("0.01"),
                filled_qty=Decimal("0"),
                reduce_only=False,
                status="NEW",
                ts=1000000,
            )
            for i in range(1, 5)
        )
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="BOTH",
                    qty=Decimal("0"),
                    entry_price=Decimal("0"),
                    mark_price=Decimal("50010"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=2000000,
                ),
            ),
            open_orders=old_orders,
            ts=2000000,
            source="test",
        )

    def test_unfreeze_resets_anchor_allows_grid_shift(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After position closes to 0, anchor resets → GRID_SHIFT passes through."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch, min_move_bps=50)

        # Tick 1: empty exchange, build grid, set anchor at mid=50000
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )
        engine.process_snapshot(self._snap("50000"))
        assert "BTCUSDT" in engine._grid_anchor_mid, "Anchor should be set"

        # Tick 2: position open → grid frozen
        engine._last_account_snapshot = self._pos_snapshot("0.01")
        out_frozen = engine.process_snapshot(self._snap("50010"))
        grid_actions_frozen = [
            la for la in out_frozen.live_actions if la.action.reason in ("GRID_SHIFT", "GRID_FILL")
        ]
        assert grid_actions_frozen == [], "Frozen: no grid actions"

        # Tick 3: position closes to 0 → unfreeze, mid moved only 2bps (50000 → 50010)
        # Without fix: GRID_SHIFT suppressed (2bps < 50bps threshold)
        # With fix: anchor reset → treated as first-time → all actions pass through
        engine._last_account_snapshot = self._flat_snapshot_with_orders()
        out_unfrozen = engine.process_snapshot(self._snap("50010"))
        shift_actions = [la for la in out_unfrozen.live_actions if la.action.reason == "GRID_SHIFT"]
        assert len(shift_actions) > 0, (
            "After unfreeze, GRID_SHIFT should pass through (anchor was reset)"
        )

    def test_no_unfreeze_without_prior_freeze(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without prior freeze, small move still suppressed (normal anti-churn)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch, min_move_bps=50)

        # Tick 1: build grid at mid=50000
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )
        engine.process_snapshot(self._snap("50000"))

        # Tick 2: small move (5bps), stale orders — should still be suppressed
        engine._last_account_snapshot = self._flat_snapshot_with_orders()
        out = engine.process_snapshot(self._snap("50025"))
        shift_actions = [la for la in out.live_actions if la.action.reason == "GRID_SHIFT"]
        assert shift_actions == [], "Without prior freeze, small move still suppressed"

    def test_anchor_reset_is_one_shot(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After unfreeze recenter, subsequent small moves are suppressed again."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch, min_move_bps=50)

        # Tick 1: build grid
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )
        engine.process_snapshot(self._snap("50000"))

        # Tick 2: freeze (position open)
        engine._last_account_snapshot = self._pos_snapshot("0.01")
        engine.process_snapshot(self._snap("50010"))

        # Tick 3: unfreeze → anchor reset → grid shifts pass through
        engine._last_account_snapshot = self._flat_snapshot_with_orders()
        out_recenter = engine.process_snapshot(self._snap("50010"))
        shift_first = [la for la in out_recenter.live_actions if la.action.reason == "GRID_SHIFT"]
        assert len(shift_first) > 0, "Recenter after unfreeze"

        # Tick 4: anchor now set to 50010, small move to 50015 (1bps) → suppressed again
        out_after = engine.process_snapshot(self._snap("50015"))
        shift_second = [la for la in out_after.live_actions if la.action.reason == "GRID_SHIFT"]
        assert shift_second == [], "After recenter, anti-churn resumes normally"


class TestTpRenewAtomic:
    """PR-P0-TP-RENEW-ATOMIC: PLACE-first renew prevents TP loss when gates block."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> LiveEngineV0:
        # Paper engine path (no live planner) so mock actions are used
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
        )

    def test_tp_renew_cancel_skipped_when_place_blocked(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If TP_RENEW PLACE is blocked by gates, CANCEL is skipped → old TP stays."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )

        # Simulate: cycle layer emits [PLACE, CANCEL] for TP_RENEW
        # PLACE will be blocked by max-position gate
        place_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50100"),
            quantity=Decimal("0.002"),
            reason="TP_RENEW",
            reduce_only=True,
            client_order_id="grinder_tp_BTCUSDT_1_2000000_2",
        )
        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol="BTCUSDT",
            order_id="grinder_tp_BTCUSDT_1_1000000_1",
            reason="TP_RENEW",
        )

        # Force PLACE to be blocked: position notional exceeds max cap
        engine._config.max_position_usd = 1.0
        engine._position_notional_usd = 1000.0

        # Inject actions as if paper_output.actions contained them
        mock_paper_engine.process_snapshot.return_value = MagicMock(
            actions=[place_action, cancel_action]
        )
        snap = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snap)

        # PLACE should be blocked
        place_results = [
            la for la in output.live_actions if la.action.action_type == ActionType.PLACE
        ]
        assert len(place_results) == 1
        assert place_results[0].status == LiveActionStatus.BLOCKED

        # CANCEL should also be blocked (skipped) to keep old TP alive
        cancel_results = [
            la for la in output.live_actions if la.action.action_type == ActionType.CANCEL
        ]
        assert len(cancel_results) == 1
        assert cancel_results[0].status == LiveActionStatus.BLOCKED
        assert cancel_results[0].block_reason == BlockReason.TP_RENEW_PLACE_FAILED

    def test_tp_renew_cancel_executes_when_place_ok(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If TP_RENEW PLACE succeeds, CANCEL proceeds normally."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )

        place_action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("50100"),
            quantity=Decimal("0.002"),
            reason="TP_RENEW",
            reduce_only=True,
            client_order_id="grinder_tp_BTCUSDT_1_2000000_2",
        )
        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol="BTCUSDT",
            order_id="grinder_tp_BTCUSDT_1_1000000_1",
            reason="TP_RENEW",
        )

        mock_paper_engine.process_snapshot.return_value = MagicMock(
            actions=[place_action, cancel_action]
        )
        snap = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snap)

        # Both PLACE and CANCEL should execute
        place_results = [
            la for la in output.live_actions if la.action.action_type == ActionType.PLACE
        ]
        cancel_results = [
            la for la in output.live_actions if la.action.action_type == ActionType.CANCEL
        ]
        assert len(place_results) == 1
        assert place_results[0].status == LiveActionStatus.EXECUTED
        assert len(cancel_results) == 1
        # CANCEL executes (even if the order doesn't exist on exchange, it's attempted)
        assert cancel_results[0].status in (
            LiveActionStatus.EXECUTED,
            LiveActionStatus.FAILED,
        )

    def test_non_tp_renew_cancel_not_affected(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CANCEL with reason != TP_RENEW is never skipped by this guard."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )

        cancel_action = ExecutionAction(
            action_type=ActionType.CANCEL,
            symbol="BTCUSDT",
            order_id="grinder_d_BTCUSDT_1_1000000_1",
            reason="TP_SLOT_TAKEOVER",
        )

        mock_paper_engine.process_snapshot.return_value = MagicMock(actions=[cancel_action])
        snap = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000"),
            last_qty=Decimal("0.5"),
        )
        output = engine.process_snapshot(snap)

        # Non-TP_RENEW CANCEL should proceed normally
        cancel_results = [
            la for la in output.live_actions if la.action.action_type == ActionType.CANCEL
        ]
        assert len(cancel_results) == 1
        assert cancel_results[0].status != LiveActionStatus.BLOCKED


class TestOrderBudgetLatch:
    """Tests for order budget exhaustion latch."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(tick_size=Decimal("0.10"), levels=2, size_per_level=Decimal("0.01"))
        )
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
        )

    def test_budget_exhausted_suppresses_planner(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        sample_snapshot: Snapshot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After budget latch → planner suppressed, no new actions."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )

        # Manually set the latch (simulates port returning "Order count limit")
        engine._order_budget_exhausted = True

        out = engine.process_snapshot(sample_snapshot)
        assert out.live_actions == [], "Planner should be suppressed when budget exhausted"

    def test_budget_latch_from_error(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ConnectorNonRetryableError with 'Order count limit' sets latch."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(), open_orders=(), ts=1000000, source="test"
        )

        assert engine._order_budget_exhausted is False

        # Simulate the error via _process_action with a PLACE that would trigger it
        place = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            reason="GRID_FILL",
        )

        # Patch _execute_single to raise the budget error
        with patch.object(
            engine,
            "_execute_single",
            side_effect=ConnectorNonRetryableError(
                "Order count limit reached: 30 orders per run. "
                "Reset port or create new instance to place more orders."
            ),
        ):
            result = engine._process_action(place, ts=1000000)

        assert result.status == LiveActionStatus.FAILED
        assert engine._order_budget_exhausted is True


class TestDetectTpFillEvent:
    """Tests for _detect_tp_fill_event (position magnitude decrease detection)."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_REPLENISH_ON_TP_FILL", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(
                tick_size=Decimal("0.10"),
                levels=2,
                size_per_level=Decimal("0.01"),
                base_spacing_bps=10.0,
            )
        )
        cycle_layer = LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer,
        )

    def test_long_position_decreases(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prev > 0 and cur >= 0 and cur < prev → True (TP filled some)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("0.05")
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("0.03")) is True

    def test_long_position_increases(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prev > 0 and cur > prev → False (position grew, not a TP fill)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("0.03")
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("0.05")) is False

    def test_long_closes_to_zero(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prev > 0 and cur == 0 → True (TP closed entire position)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("0.05")
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("0")) is True

    def test_short_position_decreases(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prev < 0 and cur <= 0 and abs(cur) < abs(prev) → True."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("-0.05")
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("-0.03")) is True

    def test_short_closes_to_zero(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prev < 0 and cur == 0 → True (TP closed entire short)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("-0.05")
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("0")) is True

    def test_no_previous_position(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No previous pos (defaults to 0), entering position → False."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("0.01")) is False

    def test_none_pos_qty(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos_qty=None → False (unknown position, skip)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("0.05")
        assert engine._detect_tp_fill_event("BTCUSDT", None) is False

    def test_flat_stays_flat(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """prev == 0 and cur == 0 → False."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._prev_pos_qty["BTCUSDT"] = Decimal("0")
        assert engine._detect_tp_fill_event("BTCUSDT", Decimal("0")) is False


class TestUpdateGridAnchors:
    """Tests for _update_grid_anchors (anchor management when flat)."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_REPLENISH_ON_TP_FILL", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(
                tick_size=Decimal("0.10"),
                levels=2,
                size_per_level=Decimal("0.01"),
                base_spacing_bps=10.0,
            )
        )
        cycle_layer = LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer,
        )

    def test_anchors_set_when_flat(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos_qty == 0 with BUY/SELL orders → anchors stored."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49900"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49800"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_3_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50100"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_4_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50200"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )

        engine._update_grid_anchors("BTCUSDT", Decimal("0"))

        assert engine._grid_anchor_low_buy["BTCUSDT"] == Decimal("49800")
        assert engine._grid_anchor_high_sell["BTCUSDT"] == Decimal("50200")

    def test_anchors_not_updated_when_position_open(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos_qty != 0 → anchors NOT updated."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49900"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )

        engine._update_grid_anchors("BTCUSDT", Decimal("0.01"))

        assert "BTCUSDT" not in engine._grid_anchor_low_buy

    def test_tp_orders_excluded_from_anchors(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TP orders (strategy_id='tp') excluded from anchor calculation."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = AccountSnapshot(
            positions=(),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49900"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_tp_BTCUSDT_0_2000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50300"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=True,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50100"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )

        engine._update_grid_anchors("BTCUSDT", Decimal("0"))

        # TP SELL at 50300 excluded; grid SELL at 50100 used
        assert engine._grid_anchor_high_sell["BTCUSDT"] == Decimal("50100")
        assert engine._grid_anchor_low_buy["BTCUSDT"] == Decimal("49900")


class TestGenerateTpFillReplenish:
    """Tests for _generate_tp_fill_replenish (BUY below + SELL above on TP fill)."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
        *,
        replenish_enabled: bool = True,
        base_spacing_bps: float = 10.0,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")
        if replenish_enabled:
            monkeypatch.setenv("GRINDER_LIVE_REPLENISH_ON_TP_FILL", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(
                tick_size=Decimal("0.10"),
                levels=2,
                size_per_level=Decimal("0.01"),
                base_spacing_bps=base_spacing_bps,
            )
        )
        cycle_layer = LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer,
        )

    @staticmethod
    def _snapshot_with_orders(
        *,
        buy_prices: tuple[str, ...] = (),
        sell_prices: tuple[str, ...] = (),
        pos_qty: str = "0.01",
    ) -> AccountSnapshot:
        orders: list[OpenOrderSnap] = []
        for i, p in enumerate(buy_prices, 1):
            orders.append(
                OpenOrderSnap(
                    order_id=f"grinder_d_BTCUSDT_{i}_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal(p),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                )
            )
        for i, p in enumerate(sell_prices, len(buy_prices) + 1):
            orders.append(
                OpenOrderSnap(
                    order_id=f"grinder_d_BTCUSDT_{i}_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal(p),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                )
            )
        positions: tuple[PositionSnap, ...] = ()
        if Decimal(pos_qty) != 0:
            positions = (
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG" if Decimal(pos_qty) > 0 else "SHORT",
                    qty=Decimal(pos_qty),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000000,
                ),
            )
        return AccountSnapshot(
            positions=positions,
            open_orders=tuple(orders),
            ts=1000000,
            source="test",
        )

    def test_tp_fill_generates_buy_and_sell(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TP fills → BUY below lowest BUY + SELL above highest SELL."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = self._snapshot_with_orders(
            buy_prices=("49900.0", "49800.0"),
            sell_prices=("50100.0", "50200.0"),
            pos_qty="0.01",
        )

        actions = engine._generate_tp_fill_replenish("BTCUSDT", Decimal("0.01"), 1000000)

        assert len(actions) == 2
        buy_action = next(a for a in actions if a.side == OrderSide.BUY)
        sell_action = next(a for a in actions if a.side == OrderSide.SELL)
        assert buy_action.action_type == ActionType.PLACE
        assert sell_action.action_type == ActionType.PLACE
        assert buy_action.reason == "TP_FILL_REPLENISH"
        assert sell_action.reason == "TP_FILL_REPLENISH"
        assert buy_action.reduce_only is False
        assert sell_action.reduce_only is False
        # BUY below lowest: 49800 * (1 - 10/10000) = 49800 * 0.999 = 49750.2 → 49750.2
        assert buy_action.price == Decimal("49750.2")
        # SELL above highest: 50200 * (1 + 10/10000) = 50200 * 1.001 = 50250.2 → 50250.2
        assert sell_action.price == Decimal("50250.2")

    def test_disabled_returns_empty(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GRINDER_LIVE_REPLENISH_ON_TP_FILL=0 → no actions."""
        engine = self._make_engine(
            mock_paper_engine,
            noop_port,
            monkeypatch,
            replenish_enabled=False,
        )
        engine._last_account_snapshot = self._snapshot_with_orders(
            buy_prices=("49900.0",),
            sell_prices=("50100.0",),
            pos_qty="0.01",
        )

        actions = engine._generate_tp_fill_replenish("BTCUSDT", Decimal("0.01"), 1000000)
        assert actions == []

    def test_flat_position_returns_empty(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pos_qty == 0 → no replenish (no open cycle)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = self._snapshot_with_orders(
            buy_prices=("49900.0",),
            sell_prices=("50100.0",),
            pos_qty="0",
        )

        actions = engine._generate_tp_fill_replenish("BTCUSDT", Decimal("0"), 1000000)
        assert actions == []

    def test_no_anchor_no_orders_returns_empty(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No open orders + no anchors → no replenish (can't determine grid edges)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = self._snapshot_with_orders(
            buy_prices=(),
            sell_prices=(),
            pos_qty="0.01",
        )

        actions = engine._generate_tp_fill_replenish("BTCUSDT", Decimal("0.01"), 1000000)
        assert actions == []

    def test_uses_anchors_when_no_orders(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No current orders → falls back to stored anchors."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        # Pre-set anchors from when position was flat
        engine._grid_anchor_low_buy["BTCUSDT"] = Decimal("49800.0")
        engine._grid_anchor_high_sell["BTCUSDT"] = Decimal("50200.0")
        engine._last_account_snapshot = self._snapshot_with_orders(
            buy_prices=(),
            sell_prices=(),
            pos_qty="0.01",
        )

        actions = engine._generate_tp_fill_replenish("BTCUSDT", Decimal("0.01"), 1000000)

        assert len(actions) == 2
        buy_action = next(a for a in actions if a.side == OrderSide.BUY)
        sell_action = next(a for a in actions if a.side == OrderSide.SELL)
        assert buy_action.price == Decimal("49750.2")
        assert sell_action.price == Decimal("50250.2")

    def test_client_order_id_generated(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replenish actions have valid client_order_id (grinder_d_ prefix)."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)
        engine._last_account_snapshot = self._snapshot_with_orders(
            buy_prices=("49900.0",),
            sell_prices=("50100.0",),
            pos_qty="0.01",
        )

        actions = engine._generate_tp_fill_replenish("BTCUSDT", Decimal("0.01"), 1000000)

        assert len(actions) == 2
        for a in actions:
            assert a.client_order_id is not None
            assert a.client_order_id.startswith("grinder_d_")


class TestTpFillReplenishE2e:
    """End-to-end wiring tests: process_snapshot detects TP fill and generates replenish."""

    @staticmethod
    def _make_engine(
        mock_paper_engine: MagicMock,
        port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
        *,
        replenish_enabled: bool = True,
        freeze_enabled: bool = False,
    ) -> LiveEngineV0:
        monkeypatch.setenv("GRINDER_LIVE_PLANNER_ENABLED", "1")
        monkeypatch.setenv("GRINDER_ACCOUNT_SYNC_ENABLED", "1")
        monkeypatch.setenv("GRINDER_LIVE_CYCLE_ENABLED", "1")
        if replenish_enabled:
            monkeypatch.setenv("GRINDER_LIVE_REPLENISH_ON_TP_FILL", "1")
        if freeze_enabled:
            monkeypatch.setenv("GRINDER_LIVE_FREEZE_GRID_WHEN_IN_POSITION", "1")

        planner = LiveGridPlannerV1(
            LiveGridConfig(
                tick_size=Decimal("0.10"),
                levels=2,
                size_per_level=Decimal("0.01"),
                base_spacing_bps=10.0,
            )
        )
        cycle_layer = LiveCycleLayerV1(LiveCycleConfig(spacing_bps=10.0, tick_size=Decimal("0.10")))
        mock_syncer = MagicMock()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(
            mock_paper_engine,
            port,
            config,
            account_syncer=mock_syncer,
            grid_planners={"BTCUSDT": planner},
            cycle_layer=cycle_layer,
        )

    def test_full_cycle_tp_fill_replenish(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full cycle: flat → fill → TP fill → replenish actions generated."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)

        # Tick 1: Flat with grid orders → anchors set
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="BOTH",
                    qty=Decimal("0"),
                    entry_price=Decimal("0"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49900"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50100"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )
        snap1 = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snap1)
        # Verify anchors set
        assert engine._grid_anchor_low_buy.get("BTCUSDT") is not None

        # Tick 2: Position opens (BUY filled) → prev_pos_qty set
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("49900"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=2000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50100"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=2000000,
                ),
            ),
            ts=2000000,
            source="test",
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snap2)
        assert engine._prev_pos_qty["BTCUSDT"] == Decimal("0.01")

        # Tick 3: TP fills → position decreases → replenish generated
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="BOTH",
                    qty=Decimal("0"),
                    entry_price=Decimal("0"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=3000000,
                ),
            ),
            open_orders=(),
            ts=3000000,
            source="test",
        )
        snap3 = Snapshot(
            ts=3000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out3 = engine.process_snapshot(snap3)

        # Position went to 0 → _detect_tp_fill_event fires → BUT pos_qty=0
        # means _generate_tp_fill_replenish returns [] (no open cycle)
        # This is correct: fully closed position = no replenish needed
        replenish_actions = [
            la for la in out3.live_actions if la.action.reason == "TP_FILL_REPLENISH"
        ]
        assert replenish_actions == [], "No replenish when position fully closed"

    def test_partial_tp_fill_generates_replenish(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Partial TP fill (pos decreases but > 0) → replenish generated."""
        engine = self._make_engine(mock_paper_engine, noop_port, monkeypatch)

        # Tick 1: Position open with orders
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.02"),
                    entry_price=Decimal("49900"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("2"),
                    leverage=1,
                    ts=1000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49800"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50200"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )
        snap1 = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snap1)

        # Tick 2: TP partially fills → position decreases to 0.01
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("49900"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=2000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49800"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=2000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50200"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=2000000,
                ),
            ),
            ts=2000000,
            source="test",
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out2 = engine.process_snapshot(snap2)

        replenish_actions = [
            la for la in out2.live_actions if la.action.reason == "TP_FILL_REPLENISH"
        ]
        assert len(replenish_actions) == 2, (
            f"Expected 2 replenish actions, got {len(replenish_actions)}"
        )
        sides = {la.action.side for la in replenish_actions}
        assert OrderSide.BUY in sides
        assert OrderSide.SELL in sides

    def test_tp_fill_replenish_not_blocked_by_freeze(
        self,
        mock_paper_engine: MagicMock,
        noop_port: NoOpExchangePort,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TP_FILL_REPLENISH reason is NOT filtered by grid freeze (only REPLENISH is)."""
        engine = self._make_engine(
            mock_paper_engine,
            noop_port,
            monkeypatch,
            freeze_enabled=True,
        )

        # Tick 1: pos=0.02 with orders
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.02"),
                    entry_price=Decimal("49900"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("2"),
                    leverage=1,
                    ts=1000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49800"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50200"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=1000000,
                ),
            ),
            ts=1000000,
            source="test",
        )
        snap1 = Snapshot(
            ts=1000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        engine.process_snapshot(snap1)

        # Tick 2: TP partially fills → pos=0.01, freeze ON
        engine._last_account_snapshot = AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("49900"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("1"),
                    leverage=1,
                    ts=2000000,
                ),
            ),
            open_orders=(
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_1_1000_1",
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    price=Decimal("49800"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=2000000,
                ),
                OpenOrderSnap(
                    order_id="grinder_d_BTCUSDT_2_1000_1",
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    price=Decimal("50200"),
                    qty=Decimal("0.01"),
                    filled_qty=Decimal("0"),
                    reduce_only=False,
                    status="NEW",
                    ts=2000000,
                ),
            ),
            ts=2000000,
            source="test",
        )
        snap2 = Snapshot(
            ts=2000000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50000.5"),
            last_qty=Decimal("0.5"),
        )
        out2 = engine.process_snapshot(snap2)

        # TP_FILL_REPLENISH has different reason from "REPLENISH" — not filtered by freeze
        replenish_actions = [
            la for la in out2.live_actions if la.action.reason == "TP_FILL_REPLENISH"
        ]
        assert len(replenish_actions) == 2, (
            f"TP_FILL_REPLENISH should pass through freeze, got {len(replenish_actions)}"
        )


# --- K) Reduce-only enforcement (PR-ROLL-1, 10 tests) ---


class TestReduceOnlyEnforcement:
    """Tests for _enforce_reduce_only() safety enforcement."""

    @staticmethod
    def _make_engine(port: Any = None) -> LiveEngineV0:
        """Create a minimal engine for enforcement testing."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        if port is None:
            port = NoOpExchangePort()
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        return LiveEngineV0(paper, port, config)

    @staticmethod
    def _long_snapshot(symbol: str = "BTCUSDT", qty: str = "0.01") -> AccountSnapshot:
        """Create AccountSnapshot with a LONG position."""
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol=symbol,
                    side="LONG",
                    qty=Decimal(qty),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

    @staticmethod
    def _short_snapshot(symbol: str = "BTCUSDT", qty: str = "0.01") -> AccountSnapshot:
        """Create AccountSnapshot with a SHORT position."""
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol=symbol,
                    side="SHORT",
                    qty=Decimal(qty),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

    @staticmethod
    def _flat_snapshot(symbol: str = "BTCUSDT") -> AccountSnapshot:
        """Create AccountSnapshot with flat position (qty=0)."""
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol=symbol,
                    side="LONG",
                    qty=Decimal("0"),
                    entry_price=Decimal("0"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

    def test_long_position_sell_enforced(self) -> None:
        """pos=LONG, SELL PLACE -> reduce_only becomes True."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")
        assert pos_sign == 1

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is True
        assert action.reduce_only is True

    def test_long_position_buy_unchanged(self) -> None:
        """pos=LONG, BUY PLACE -> reduce_only stays False (same side)."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False
        assert action.reduce_only is False

    def test_short_position_buy_enforced(self) -> None:
        """pos=SHORT, BUY PLACE -> reduce_only becomes True."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._short_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")
        assert pos_sign == -1

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is True
        assert action.reduce_only is True

    def test_short_position_sell_unchanged(self) -> None:
        """pos=SHORT, SELL PLACE -> reduce_only stays False (same side)."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._short_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False
        assert action.reduce_only is False

    def test_flat_no_enforcement(self) -> None:
        """pos=flat (qty=0) -> reduce_only unchanged."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._flat_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")
        assert pos_sign is None  # flat returns None

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False
        assert action.reduce_only is False

    def test_pos_sign_none_no_enforcement(self) -> None:
        """No snapshot / pos_sign=None -> no enforcement."""
        engine = self._make_engine()
        # No snapshot set -> _get_position_sign returns None
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        result = engine._enforce_reduce_only(action, None)

        assert result is False
        assert action.reduce_only is False

    def test_cancel_skipped(self) -> None:
        """CANCEL action -> never enforced regardless of position."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.CANCEL,
            order_id="ORDER_123",
            symbol="BTCUSDT",
        )
        pos_sign = engine._get_position_sign("BTCUSDT")
        assert pos_sign == 1

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False

    def test_already_reduce_only_no_double_count(self) -> None:
        """Action with reduce_only=True -> no metric increment, no log."""
        reset_live_engine_metrics()
        engine = self._make_engine()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
            reduce_only=True,  # already set (e.g. TP order)
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False
        assert action.reduce_only is True  # unchanged
        metrics = get_live_engine_metrics()
        assert len(metrics.reduce_only_enforced) == 0  # counter=0
        reset_live_engine_metrics()

    def test_metric_recorded(self) -> None:
        """Verify counter incremented with correct {sym, side, reason}."""
        reset_live_engine_metrics()
        engine = self._make_engine()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        engine._enforce_reduce_only(action, pos_sign)

        metrics = get_live_engine_metrics()
        key = ("BTCUSDT", "SELL", "position_long")
        assert metrics.reduce_only_enforced.get(key) == 1
        reset_live_engine_metrics()

    def test_replace_enforced(self) -> None:
        """REPLACE action also gets reduce_only=True when position open."""
        engine = self._make_engine()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.REPLACE,
            order_id="ORDER_123",
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is True
        assert action.reduce_only is True


# --- L) Reduce-only enforcement toggle (PR-ROLL-1b, 4 tests) ---


class TestReduceOnlyEnforcementToggle:
    """Tests for GRINDER_LIVE_REDUCE_ONLY_ENFORCEMENT toggle."""

    @staticmethod
    def _make_engine_disabled() -> LiveEngineV0:
        """Create engine with enforcement disabled."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, NoOpExchangePort(), config)
        engine._reduce_only_enforcement = False
        return engine

    @staticmethod
    def _long_snapshot() -> AccountSnapshot:
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

    @staticmethod
    def _short_snapshot() -> AccountSnapshot:
        return AccountSnapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="SHORT",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("50000"),
                    mark_price=Decimal("50000"),
                    unrealized_pnl=Decimal("0"),
                    leverage=1,
                    ts=1000,
                ),
            ),
            open_orders=(),
            ts=1000,
            source="test",
        )

    def test_disabled_long_sell_not_forced(self) -> None:
        """Enforcement disabled + LONG + SELL -> reduce_only stays False."""
        engine = self._make_engine_disabled()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")
        assert pos_sign == 1

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False
        assert action.reduce_only is False

    def test_disabled_short_buy_not_forced(self) -> None:
        """Enforcement disabled + SHORT + BUY -> reduce_only stays False."""
        engine = self._make_engine_disabled()
        engine._last_account_snapshot = self._short_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("49000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")
        assert pos_sign == -1

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is False
        assert action.reduce_only is False

    def test_disabled_no_metric(self) -> None:
        """Enforcement disabled -> no metric increment."""
        reset_live_engine_metrics()
        engine = self._make_engine_disabled()
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        engine._enforce_reduce_only(action, pos_sign)

        metrics = get_live_engine_metrics()
        assert len(metrics.reduce_only_enforced) == 0
        reset_live_engine_metrics()

    def test_enabled_still_enforces(self) -> None:
        """Enforcement enabled (default) -> LONG + SELL gets reduce_only=True."""
        paper = MagicMock()
        paper.process_snapshot.return_value = MagicMock(actions=[])
        config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
        engine = LiveEngineV0(paper, NoOpExchangePort(), config)
        assert engine._reduce_only_enforcement is True
        engine._last_account_snapshot = self._long_snapshot()
        action = ExecutionAction(
            action_type=ActionType.PLACE,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=Decimal("51000"),
            quantity=Decimal("0.01"),
        )
        pos_sign = engine._get_position_sign("BTCUSDT")

        result = engine._enforce_reduce_only(action, pos_sign)

        assert result is True
        assert action.reduce_only is True
