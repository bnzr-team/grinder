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
from unittest.mock import MagicMock

import pytest

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
from grinder.live import (
    BlockReason,
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineV0,
    classify_intent,
)
from grinder.live.fsm_driver import FsmDriver
from grinder.live.fsm_metrics import get_fsm_metrics, reset_fsm_metrics
from grinder.live.fsm_orchestrator import FsmConfig, OrchestratorFSM
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
    """Tests for Gate 6: FSM state-based intent blocking."""

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
        """FSM in PAUSED → CANCEL allowed (reduce-risk intents pass Gate 6)."""
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
        """GRINDER_OPERATOR_OVERRIDE=PAUSE → ACTIVE→PAUSED → PLACE blocked by Gate 6.

        This also proves tick-before-gate ordering: the FSM transitions in
        the same snapshot that contains the PLACE action, and Gate 6 sees
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
        # PLACE blocked by Gate 6 (not Gate 3 — kill switch is off)
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
        """No active signals → FSM stays ACTIVE → PLACE passes Gate 6."""
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
        """' pause ' (whitespace + lowercase) → normalized to PAUSE → Gate 6 blocks."""
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
        assert output_dict["paper_output"]["deferred_by_fsm"] is True
        assert output_dict["paper_output"]["actions"] == []
