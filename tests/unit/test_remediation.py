"""Tests for active remediation (LC-10).

Tests cover:
- 9 safety gates (one test per gate)
- Execution (cancel + flatten + limits)
- Kill-switch semantics (allows remediation)
- Metrics (planned, executed, blocked counters)
"""

import os
import time
from collections.abc import Generator
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.core import OrderSide, OrderState
from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.metrics import get_reconcile_metrics, reset_reconcile_metrics
from grinder.reconcile.remediation import (
    GRINDER_PREFIX,
    RemediationBlockReason,
    RemediationExecutor,
    RemediationResult,
    RemediationStatus,
)
from grinder.reconcile.types import ObservedOrder, ObservedPosition


@pytest.fixture
def mock_port() -> MagicMock:
    """Create mock BinanceFuturesPort."""
    port = MagicMock()
    port.cancel_order.return_value = True
    port.place_market_order.return_value = "grinder_BTCUSDT_cleanup_123_1"
    return port


@pytest.fixture
def observed_order() -> ObservedOrder:
    """Create a sample observed order with grinder_ prefix."""
    return ObservedOrder(
        client_order_id="grinder_BTCUSDT_1_1704067200000_1",
        symbol="BTCUSDT",
        order_id=12345678,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("42500.00"),
        orig_qty=Decimal("0.010"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=1704067200000,
    )


@pytest.fixture
def observed_order_no_prefix() -> ObservedOrder:
    """Create a sample observed order WITHOUT grinder_ prefix."""
    return ObservedOrder(
        client_order_id="manual_order_12345",
        symbol="BTCUSDT",
        order_id=99999999,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("42500.00"),
        orig_qty=Decimal("0.010"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=1704067200000,
    )


@pytest.fixture
def observed_position() -> ObservedPosition:
    """Create a sample observed position."""
    return ObservedPosition(
        symbol="BTCUSDT",
        position_amt=Decimal("0.010"),
        entry_price=Decimal("42500.00"),
        unrealized_pnl=Decimal("10.00"),
        ts_observed=1704067200000,
    )


@pytest.fixture
def observed_position_large() -> ObservedPosition:
    """Create a large observed position (exceeds notional cap)."""
    return ObservedPosition(
        symbol="BTCUSDT",
        position_amt=Decimal("1.0"),  # 1 BTC
        entry_price=Decimal("42500.00"),
        unrealized_pnl=Decimal("1000.00"),
        ts_observed=1704067200000,
    )


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset metrics before each test."""
    reset_reconcile_metrics()


@pytest.fixture(autouse=True)
def clean_env() -> Generator[None, None, None]:
    """Clean environment variable before each test."""
    os.environ.pop("ALLOW_MAINNET_TRADE", None)
    yield
    os.environ.pop("ALLOW_MAINNET_TRADE", None)


def _make_executor(
    mock_port: MagicMock,
    *,
    action: RemediationAction = RemediationAction.CANCEL_ALL,
    dry_run: bool = True,
    allow_active: bool = False,
    armed: bool = False,
    whitelist: list[str] | None = None,
    cooldown_seconds: int = 60,
    max_orders: int = 10,
    max_symbols: int = 3,
    max_notional: Decimal = Decimal("500"),
    require_whitelist: bool = True,
    kill_switch: bool = False,
) -> RemediationExecutor:
    """Helper to create RemediationExecutor with specific config."""
    config = ReconcileConfig(
        action=action,
        dry_run=dry_run,
        allow_active_remediation=allow_active,
        cooldown_seconds=cooldown_seconds,
        max_orders_per_action=max_orders,
        max_symbols_per_action=max_symbols,
        max_flatten_notional_usdt=max_notional,
        require_whitelist=require_whitelist,
    )
    return RemediationExecutor(
        config=config,
        port=mock_port,
        armed=armed,
        symbol_whitelist=whitelist or [],
        kill_switch_active=kill_switch,
    )


# =============================================================================
# Safety Gate Tests (9 tests, one per gate)
# =============================================================================


class TestRemediationSafetyGates:
    """Tests for all 9 safety gates."""

    def test_action_none_returns_planned(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 1: action=NONE → PLANNED (dry-run behavior)."""
        executor = _make_executor(mock_port, action=RemediationAction.NONE)

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.PLANNED
        assert result.block_reason == RemediationBlockReason.ACTION_IS_NONE
        mock_port.cancel_order.assert_not_called()

    def test_dry_run_true_returns_planned(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 2: dry_run=True → PLANNED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=True,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.PLANNED
        assert result.block_reason == RemediationBlockReason.DRY_RUN
        mock_port.cancel_order.assert_not_called()

    def test_allow_active_false_returns_blocked(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 3: allow_active_remediation=False → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=False,  # Gate fails here
            armed=True,
            whitelist=["BTCUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NOT_ALLOWED
        mock_port.cancel_order.assert_not_called()

    def test_armed_false_returns_blocked(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 4: armed=False → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=False,  # Gate fails here
            whitelist=["BTCUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NOT_ARMED
        mock_port.cancel_order.assert_not_called()

    def test_env_var_missing_returns_blocked(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 5: ALLOW_MAINNET_TRADE not set → BLOCKED."""
        # Do NOT set env var
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
        )

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.ENV_VAR_MISSING
        mock_port.cancel_order.assert_not_called()

    def test_cooldown_not_elapsed_returns_blocked(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 6: cooldown not elapsed → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            cooldown_seconds=3600,  # 1 hour cooldown
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        # Simulate recent action
        executor._last_action_ts = int(time.time() * 1000)

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.COOLDOWN_NOT_ELAPSED
        mock_port.cancel_order.assert_not_called()

    def test_symbol_not_whitelisted_returns_blocked(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Gate 7: symbol not in whitelist → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["ETHUSDT"],  # BTCUSDT not whitelisted
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.SYMBOL_NOT_IN_WHITELIST
        mock_port.cancel_order.assert_not_called()

    def test_no_grinder_prefix_returns_blocked(
        self, mock_port: MagicMock, observed_order_no_prefix: ObservedOrder
    ) -> None:
        """Gate 8: client_order_id without grinder_ prefix → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order_no_prefix)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NO_GRINDER_PREFIX
        mock_port.cancel_order.assert_not_called()

    def test_notional_exceeds_limit_returns_blocked(
        self, mock_port: MagicMock, observed_position_large: ObservedPosition
    ) -> None:
        """Gate 9: notional > max_flatten_notional_usdt → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.FLATTEN,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            max_notional=Decimal("500"),  # 1 BTC @ 42500 = $42500 >> $500
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        # Price is $42500, position is 1.0 BTC, notional = $42500
        result = executor.remediate_flatten(
            observed_position_large, current_price=Decimal("42500.00")
        )

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NOTIONAL_EXCEEDS_LIMIT
        mock_port.place_market_order.assert_not_called()


# =============================================================================
# Execution Tests (4 tests)
# =============================================================================


class TestRemediationExecution:
    """Tests for successful execution paths."""

    def test_cancel_all_gates_pass_executes(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """All gates pass → cancel executed."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.EXECUTED
        assert result.block_reason is None
        assert result.action == "cancel_all"
        mock_port.cancel_order.assert_called_once_with(observed_order.client_order_id)

    def test_flatten_all_gates_pass_executes(
        self, mock_port: MagicMock, observed_position: ObservedPosition
    ) -> None:
        """All gates pass → flatten executed."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.FLATTEN,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            max_notional=Decimal("1000"),  # 0.01 BTC @ 42500 = $425 < $1000
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_flatten(observed_position, current_price=Decimal("42500.00"))

        assert result.status == RemediationStatus.EXECUTED
        assert result.block_reason is None
        assert result.action == "flatten"
        # Position is long (positive), so need to SELL to close
        mock_port.place_market_order.assert_called_once_with(
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.010"),
            reduce_only=True,
        )

    def test_max_orders_per_action_enforced(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Hits max_orders_per_action limit → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            max_orders=2,
            cooldown_seconds=0,  # Disable cooldown for this test
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        # Execute 2 orders
        executor.remediate_cancel(observed_order)
        executor.remediate_cancel(observed_order)

        # Third should be blocked
        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.MAX_ORDERS_REACHED

    def test_max_symbols_per_action_enforced(self, mock_port: MagicMock) -> None:
        """Hits max_symbols_per_action limit → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
            max_symbols=2,
            cooldown_seconds=0,  # Disable cooldown for this test
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        # Execute for 2 different symbols
        order1 = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            symbol="BTCUSDT",
            order_id=1,
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("42500"),
            orig_qty=Decimal("0.01"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1,
        )
        order2 = ObservedOrder(
            client_order_id="grinder_ETHUSDT_1_1704067200000_1",
            symbol="ETHUSDT",
            order_id=2,
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("2500"),
            orig_qty=Decimal("0.1"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1,
        )
        order3 = ObservedOrder(
            client_order_id="grinder_SOLUSDT_1_1704067200000_1",
            symbol="SOLUSDT",
            order_id=3,
            side=OrderSide.BUY,
            status=OrderState.OPEN,
            price=Decimal("100"),
            orig_qty=Decimal("1"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1,
        )

        executor.remediate_cancel(order1)  # BTCUSDT
        executor.remediate_cancel(order2)  # ETHUSDT

        # Third symbol should be blocked
        result = executor.remediate_cancel(order3)  # SOLUSDT

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.MAX_SYMBOLS_REACHED


# =============================================================================
# Kill-switch Tests (2 tests)
# =============================================================================


class TestRemediationKillSwitch:
    """Tests for kill-switch semantics (remediation is ALLOWED under kill-switch)."""

    def test_kill_switch_allows_cancel(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """kill_switch=True + cancel → allowed (reduces risk)."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            kill_switch=True,  # Kill-switch active
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        # Kill-switch allows remediation (it reduces risk)
        assert result.status == RemediationStatus.EXECUTED
        mock_port.cancel_order.assert_called_once()

    def test_kill_switch_allows_flatten(
        self, mock_port: MagicMock, observed_position: ObservedPosition
    ) -> None:
        """kill_switch=True + flatten → allowed (reduces risk)."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.FLATTEN,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            max_notional=Decimal("1000"),
            kill_switch=True,  # Kill-switch active
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_flatten(observed_position, current_price=Decimal("42500.00"))

        # Kill-switch allows remediation (it reduces risk)
        assert result.status == RemediationStatus.EXECUTED
        mock_port.place_market_order.assert_called_once()


# =============================================================================
# Metrics Tests (3 tests)
# =============================================================================


class TestRemediationMetrics:
    """Tests for metrics recording."""

    def test_dry_run_increments_planned_counter(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Dry-run increments action_planned counter."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=True,  # Dry-run mode
        )

        executor.remediate_cancel(observed_order)

        metrics = get_reconcile_metrics()
        assert metrics.action_planned_counts.get("cancel_all", 0) == 1
        assert metrics.action_executed_counts.get("cancel_all", 0) == 0
        assert sum(metrics.action_blocked_counts.values()) == 0

    def test_real_execution_increments_executed_counter(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Real execution increments action_executed counter."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor.remediate_cancel(observed_order)

        metrics = get_reconcile_metrics()
        assert metrics.action_executed_counts.get("cancel_all", 0) == 1
        assert metrics.action_planned_counts.get("cancel_all", 0) == 0

    def test_blocked_increments_blocked_counter_with_reason(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Blocked increments action_blocked counter with reason."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=False,  # This will cause block
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor.remediate_cancel(observed_order)

        metrics = get_reconcile_metrics()
        assert metrics.action_blocked_counts.get("allow_active_remediation_false", 0) == 1


# =============================================================================
# Additional Tests
# =============================================================================


class TestRemediationResult:
    """Tests for RemediationResult."""

    def test_to_log_extra(self) -> None:
        """Test to_log_extra returns correct fields."""
        result = RemediationResult(
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1704067200000_1",
            status=RemediationStatus.EXECUTED,
            action="cancel_all",
        )

        extra = result.to_log_extra()

        assert extra["mismatch_type"] == "ORDER_EXISTS_UNEXPECTED"
        assert extra["symbol"] == "BTCUSDT"
        assert extra["client_order_id"] == "grinder_BTCUSDT_1_1704067200000_1"
        assert extra["status"] == "executed"
        assert extra["action"] == "cancel_all"
        assert extra["block_reason"] is None
        assert extra["error"] is None

    def test_to_log_extra_with_block_reason(self) -> None:
        """Test to_log_extra includes block_reason when present."""
        result = RemediationResult(
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            symbol="BTCUSDT",
            client_order_id="manual_order_123",
            status=RemediationStatus.BLOCKED,
            block_reason=RemediationBlockReason.NO_GRINDER_PREFIX,
            action="cancel_all",
        )

        extra = result.to_log_extra()

        assert extra["status"] == "blocked"
        assert extra["block_reason"] == "no_grinder_prefix"


class TestRemediationExecutorReset:
    """Tests for reset_run_counters."""

    def test_reset_run_counters_clears_state(self, mock_port: MagicMock) -> None:
        """reset_run_counters clears all per-run state."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT", "ETHUSDT"],
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        # Simulate some activity
        executor._orders_this_run = 5
        executor._symbols_this_run = {"BTCUSDT", "ETHUSDT"}

        executor.reset_run_counters()

        assert executor._orders_this_run == 0
        assert executor._symbols_this_run == set()


class TestRemediationBlockReasonValues:
    """Tests for RemediationBlockReason enum stability."""

    def test_all_block_reasons_have_string_values(self) -> None:
        """All block reasons have string values (contract test)."""
        for reason in RemediationBlockReason:
            assert isinstance(reason.value, str)

    def test_block_reason_values_are_stable(self) -> None:
        """Block reason values are as expected (contract test)."""
        expected = {
            "action_is_none",
            "dry_run",
            "allow_active_remediation_false",
            "not_armed",
            "env_var_missing",
            "cooldown_not_elapsed",
            "symbol_not_in_whitelist",
            "no_grinder_prefix",
            "notional_exceeds_limit",
            "max_orders_reached",
            "max_symbols_reached",
            "whitelist_required",
            "port_error",
        }

        actual = {r.value for r in RemediationBlockReason}

        assert actual == expected


class TestGrinderPrefix:
    """Tests for GRINDER_PREFIX constant."""

    def test_grinder_prefix_is_correct(self) -> None:
        """GRINDER_PREFIX is 'grinder_'."""
        assert GRINDER_PREFIX == "grinder_"


class TestRemediationStatusValues:
    """Tests for RemediationStatus enum stability."""

    def test_status_values_are_stable(self) -> None:
        """Status values are as expected (contract test)."""
        expected = {"planned", "executed", "blocked", "failed"}
        actual = {s.value for s in RemediationStatus}
        assert actual == expected


class TestWhitelistRequired:
    """Tests for require_whitelist gate."""

    def test_whitelist_required_but_empty_returns_blocked(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """require_whitelist=True but whitelist empty → BLOCKED."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=[],  # Empty whitelist
            require_whitelist=True,
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.WHITELIST_REQUIRED

    def test_whitelist_not_required_allows_empty(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """require_whitelist=False allows empty whitelist."""
        executor = _make_executor(
            mock_port,
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=[],
            require_whitelist=False,  # Not required
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        result = executor.remediate_cancel(observed_order)

        # Should execute (or at least not be blocked by whitelist)
        assert result.status == RemediationStatus.EXECUTED


class TestFlattenShortPosition:
    """Tests for flattening short positions."""

    def test_flatten_short_position_sends_buy_order(self, mock_port: MagicMock) -> None:
        """Short position (negative) → BUY to close."""
        short_position = ObservedPosition(
            symbol="BTCUSDT",
            position_amt=Decimal("-0.010"),  # Short position
            entry_price=Decimal("42500.00"),
            unrealized_pnl=Decimal("-10.00"),
            ts_observed=1704067200000,
        )

        executor = _make_executor(
            mock_port,
            action=RemediationAction.FLATTEN,
            dry_run=False,
            allow_active=True,
            armed=True,
            whitelist=["BTCUSDT"],
            max_notional=Decimal("1000"),
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor.remediate_flatten(short_position, current_price=Decimal("42500.00"))

        # Short position needs BUY to close
        mock_port.place_market_order.assert_called_once_with(
            symbol="BTCUSDT",
            side=OrderSide.BUY,  # Buy to close short
            quantity=Decimal("0.010"),  # abs() of position
            reduce_only=True,
        )
