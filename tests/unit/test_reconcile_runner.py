"""Tests for reconciliation runner wiring (LC-11).

Tests cover:
- Routing policy (mismatch type → action mapping)
- One action type per run (cancel OR flatten, not both)
- Terminal status skip
- No-action skip (ORDER_MISSING_ON_EXCHANGE)
- Metrics updates
- ReconcileRunReport
"""

import os
from collections.abc import Generator
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from grinder.core import OrderSide, OrderState
from grinder.reconcile.metrics import get_reconcile_metrics, reset_reconcile_metrics
from grinder.reconcile.remediation import (
    RemediationExecutor,
    RemediationResult,
    RemediationStatus,
)
from grinder.reconcile.runner import (
    ACTIONABLE_STATUSES,
    NO_ACTION_MISMATCHES,
    ORDER_MISMATCHES_FOR_CANCEL,
    POSITION_MISMATCHES_FOR_FLATTEN,
    TERMINAL_STATUSES,
    ReconcileRunner,
    ReconcileRunReport,
)
from grinder.reconcile.types import (
    Mismatch,
    MismatchType,
    ObservedOrder,
    ObservedPosition,
)


@pytest.fixture
def mock_engine() -> MagicMock:
    """Create mock ReconcileEngine."""
    engine = MagicMock()
    engine.reconcile.return_value = []
    return engine


@pytest.fixture
def mock_executor() -> MagicMock:
    """Create mock RemediationExecutor."""
    executor = MagicMock(spec=RemediationExecutor)
    executor.remediate_cancel.return_value = RemediationResult(
        mismatch_type="ORDER_EXISTS_UNEXPECTED",
        symbol="BTCUSDT",
        client_order_id="grinder_BTCUSDT_1_1_1",
        status=RemediationStatus.EXECUTED,
        action="cancel_all",
    )
    executor.remediate_flatten.return_value = RemediationResult(
        mismatch_type="POSITION_NONZERO_UNEXPECTED",
        symbol="BTCUSDT",
        client_order_id=None,
        status=RemediationStatus.EXECUTED,
        action="flatten",
    )
    return executor


@pytest.fixture
def mock_observed() -> MagicMock:
    """Create mock ObservedStateStore."""
    observed = MagicMock()
    observed.get_order.return_value = ObservedOrder(
        client_order_id="grinder_BTCUSDT_1_1_1",
        symbol="BTCUSDT",
        order_id=12345,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("42500"),
        orig_qty=Decimal("0.01"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=1000,
    )
    observed.get_position.return_value = ObservedPosition(
        symbol="BTCUSDT",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("42500"),
        unrealized_pnl=Decimal("10"),
        ts_observed=1000,
    )
    return observed


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


def _make_mismatch(
    mismatch_type: MismatchType,
    symbol: str = "BTCUSDT",
    client_order_id: str | None = "grinder_BTCUSDT_1_1_1",
    observed_status: OrderState = OrderState.OPEN,
) -> Mismatch:
    """Helper to create a Mismatch."""
    observed_dict = None
    if mismatch_type in ORDER_MISMATCHES_FOR_CANCEL:
        observed_dict = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "order_id": 12345,
            "side": "BUY",
            "status": observed_status.value,
            "price": "42500",
            "orig_qty": "0.01",
            "executed_qty": "0",
            "avg_price": "0",
            "ts_observed": 1000,
        }
    elif mismatch_type in POSITION_MISMATCHES_FOR_FLATTEN:
        observed_dict = {
            "symbol": symbol,
            "position_amt": "0.01",
            "entry_price": "42500",
            "unrealized_pnl": "10",
            "ts_observed": 1000,
        }

    return Mismatch(
        mismatch_type=mismatch_type,
        symbol=symbol,
        client_order_id=client_order_id,
        expected=None,
        observed=observed_dict,
        ts_detected=1000,
        action_plan=f"would {mismatch_type.value}",
    )


# =============================================================================
# Routing Policy Constants Tests
# =============================================================================


class TestRoutingPolicyConstants:
    """Tests for routing policy SSOT constants."""

    def test_order_mismatches_for_cancel(self) -> None:
        """ORDER_MISMATCHES_FOR_CANCEL contains expected types."""
        assert MismatchType.ORDER_EXISTS_UNEXPECTED in ORDER_MISMATCHES_FOR_CANCEL
        assert MismatchType.ORDER_STATUS_DIVERGENCE in ORDER_MISMATCHES_FOR_CANCEL
        assert len(ORDER_MISMATCHES_FOR_CANCEL) == 2

    def test_position_mismatches_for_flatten(self) -> None:
        """POSITION_MISMATCHES_FOR_FLATTEN contains expected types."""
        assert MismatchType.POSITION_NONZERO_UNEXPECTED in POSITION_MISMATCHES_FOR_FLATTEN
        assert len(POSITION_MISMATCHES_FOR_FLATTEN) == 1

    def test_no_action_mismatches(self) -> None:
        """NO_ACTION_MISMATCHES contains expected types."""
        assert MismatchType.ORDER_MISSING_ON_EXCHANGE in NO_ACTION_MISMATCHES
        assert len(NO_ACTION_MISMATCHES) == 1

    def test_terminal_statuses(self) -> None:
        """TERMINAL_STATUSES contains expected order states."""
        assert OrderState.FILLED in TERMINAL_STATUSES
        assert OrderState.CANCELLED in TERMINAL_STATUSES
        assert OrderState.REJECTED in TERMINAL_STATUSES
        assert OrderState.EXPIRED in TERMINAL_STATUSES
        assert len(TERMINAL_STATUSES) == 4

    def test_actionable_statuses(self) -> None:
        """ACTIONABLE_STATUSES contains expected order states."""
        assert OrderState.OPEN in ACTIONABLE_STATUSES
        assert OrderState.PARTIALLY_FILLED in ACTIONABLE_STATUSES
        assert len(ACTIONABLE_STATUSES) == 2

    def test_routing_sets_are_disjoint(self) -> None:
        """All routing sets are disjoint (no overlap)."""
        all_types = set(MismatchType)
        cancel = set(ORDER_MISMATCHES_FOR_CANCEL)
        flatten = set(POSITION_MISMATCHES_FOR_FLATTEN)
        no_action = set(NO_ACTION_MISMATCHES)

        # No overlap between sets
        assert cancel & flatten == set()
        assert cancel & no_action == set()
        assert flatten & no_action == set()

        # All types are covered
        assert cancel | flatten | no_action == all_types


# =============================================================================
# Routing Behavior Tests
# =============================================================================


class TestRoutingBehavior:
    """Tests for mismatch → action routing."""

    def test_order_exists_unexpected_routes_to_cancel(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """ORDER_EXISTS_UNEXPECTED → cancel action."""
        mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 1
        assert len(report.flatten_results) == 0
        mock_executor.remediate_cancel.assert_called_once()

    def test_order_status_divergence_routes_to_cancel(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """ORDER_STATUS_DIVERGENCE → cancel action."""
        mismatch = _make_mismatch(MismatchType.ORDER_STATUS_DIVERGENCE)
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 1
        mock_executor.remediate_cancel.assert_called_once()

    def test_position_nonzero_unexpected_routes_to_flatten(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """POSITION_NONZERO_UNEXPECTED → flatten action."""
        mismatch = _make_mismatch(
            MismatchType.POSITION_NONZERO_UNEXPECTED,
            client_order_id=None,
        )
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.flatten_results) == 1
        assert len(report.cancel_results) == 0
        mock_executor.remediate_flatten.assert_called_once()

    def test_order_missing_on_exchange_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """ORDER_MISSING_ON_EXCHANGE → skipped (no action)."""
        mismatch = _make_mismatch(MismatchType.ORDER_MISSING_ON_EXCHANGE)
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 0
        assert len(report.flatten_results) == 0
        assert report.skipped_no_action == 1
        mock_executor.remediate_cancel.assert_not_called()
        mock_executor.remediate_flatten.assert_not_called()


# =============================================================================
# One Action Type Per Run Tests
# =============================================================================


class TestOneActionTypePerRun:
    """Tests for bounded execution (one action type per run)."""

    def test_cancel_locks_action_type(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Once cancel is chosen, flatten mismatches are skipped."""
        cancel_mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        flatten_mismatch = _make_mismatch(
            MismatchType.POSITION_NONZERO_UNEXPECTED,
            client_order_id=None,
        )
        mock_engine.reconcile.return_value = [cancel_mismatch, flatten_mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        # Cancel was processed
        assert len(report.cancel_results) == 1
        # Flatten was skipped due to action_type_locked
        assert len(report.flatten_results) == 0
        mock_executor.remediate_cancel.assert_called_once()
        mock_executor.remediate_flatten.assert_not_called()

    def test_flatten_locks_action_type(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Once flatten is chosen, cancel mismatches are skipped."""
        flatten_mismatch = _make_mismatch(
            MismatchType.POSITION_NONZERO_UNEXPECTED,
            client_order_id=None,
        )
        cancel_mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        # Flatten first
        mock_engine.reconcile.return_value = [flatten_mismatch, cancel_mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        # Flatten was processed
        assert len(report.flatten_results) == 1
        # Cancel was skipped due to action_type_locked
        assert len(report.cancel_results) == 0
        mock_executor.remediate_flatten.assert_called_once()
        mock_executor.remediate_cancel.assert_not_called()

    def test_multiple_cancels_same_run(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Multiple cancel mismatches in same run are all processed."""
        mismatch1 = _make_mismatch(
            MismatchType.ORDER_EXISTS_UNEXPECTED,
            client_order_id="grinder_BTCUSDT_1_1_1",
        )
        mismatch2 = _make_mismatch(
            MismatchType.ORDER_EXISTS_UNEXPECTED,
            client_order_id="grinder_BTCUSDT_1_1_2",
        )
        mock_engine.reconcile.return_value = [mismatch1, mismatch2]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 2
        assert mock_executor.remediate_cancel.call_count == 2


# =============================================================================
# Terminal Status Tests
# =============================================================================


class TestTerminalStatusSkip:
    """Tests for skipping terminal-status orders."""

    def test_filled_order_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """FILLED status → skip cancel."""
        mismatch = _make_mismatch(
            MismatchType.ORDER_STATUS_DIVERGENCE,
            observed_status=OrderState.FILLED,
        )
        mock_engine.reconcile.return_value = [mismatch]

        # Return terminal-status order from observed store
        mock_observed.get_order.return_value = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1_1",
            symbol="BTCUSDT",
            order_id=12345,
            side=OrderSide.BUY,
            status=OrderState.FILLED,
            price=Decimal("42500"),
            orig_qty=Decimal("0.01"),
            executed_qty=Decimal("0.01"),
            avg_price=Decimal("42500"),
            ts_observed=1000,
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 0
        assert report.skipped_terminal == 1
        mock_executor.remediate_cancel.assert_not_called()

    def test_cancelled_order_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """CANCELLED status → skip cancel."""
        mismatch = _make_mismatch(
            MismatchType.ORDER_STATUS_DIVERGENCE,
            observed_status=OrderState.CANCELLED,
        )
        mock_engine.reconcile.return_value = [mismatch]

        mock_observed.get_order.return_value = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1_1",
            symbol="BTCUSDT",
            order_id=12345,
            side=OrderSide.BUY,
            status=OrderState.CANCELLED,
            price=Decimal("42500"),
            orig_qty=Decimal("0.01"),
            executed_qty=Decimal("0"),
            avg_price=Decimal("0"),
            ts_observed=1000,
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 0
        assert report.skipped_terminal == 1

    def test_open_order_not_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """OPEN status → not skipped."""
        mismatch = _make_mismatch(
            MismatchType.ORDER_EXISTS_UNEXPECTED,
            observed_status=OrderState.OPEN,
        )
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 1
        assert report.skipped_terminal == 0

    def test_partially_filled_order_not_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """PARTIALLY_FILLED status → not skipped."""
        mismatch = _make_mismatch(
            MismatchType.ORDER_EXISTS_UNEXPECTED,
            observed_status=OrderState.PARTIALLY_FILLED,
        )
        mock_engine.reconcile.return_value = [mismatch]

        mock_observed.get_order.return_value = ObservedOrder(
            client_order_id="grinder_BTCUSDT_1_1_1",
            symbol="BTCUSDT",
            order_id=12345,
            side=OrderSide.BUY,
            status=OrderState.PARTIALLY_FILLED,
            price=Decimal("42500"),
            orig_qty=Decimal("0.01"),
            executed_qty=Decimal("0.005"),
            avg_price=Decimal("42500"),
            ts_observed=1000,
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 1
        assert report.skipped_terminal == 0


# =============================================================================
# Metrics Tests
# =============================================================================


class TestRunnerMetrics:
    """Tests for metrics updates."""

    def test_runs_with_mismatch_incremented(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """runs_with_mismatch incremented when mismatches detected."""
        mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        runner.run()

        metrics = get_reconcile_metrics()
        assert metrics.runs_with_mismatch == 1

    def test_runs_with_mismatch_not_incremented_on_empty(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """runs_with_mismatch NOT incremented when no mismatches."""
        mock_engine.reconcile.return_value = []

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        runner.run()

        metrics = get_reconcile_metrics()
        assert metrics.runs_with_mismatch == 0

    def test_runs_with_remediation_incremented_on_execute(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """runs_with_remediation incremented on real execution."""
        mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        mock_engine.reconcile.return_value = [mismatch]

        # Executor returns EXECUTED status
        mock_executor.remediate_cancel.return_value = RemediationResult(
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1_1",
            status=RemediationStatus.EXECUTED,
            action="cancel_all",
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        runner.run()

        metrics = get_reconcile_metrics()
        assert metrics.runs_with_remediation_counts.get("cancel_all", 0) == 1

    def test_runs_with_remediation_not_incremented_on_planned(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """runs_with_remediation NOT incremented on dry-run (PLANNED)."""
        mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        mock_engine.reconcile.return_value = [mismatch]

        # Executor returns PLANNED status
        mock_executor.remediate_cancel.return_value = RemediationResult(
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1_1",
            status=RemediationStatus.PLANNED,
            action="cancel_all",
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        runner.run()

        metrics = get_reconcile_metrics()
        assert metrics.runs_with_remediation_counts.get("cancel_all", 0) == 0

    def test_last_remediation_ts_updated(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """last_remediation_ts_ms updated on real execution."""
        mismatch = _make_mismatch(MismatchType.ORDER_EXISTS_UNEXPECTED)
        mock_engine.reconcile.return_value = [mismatch]

        mock_executor.remediate_cancel.return_value = RemediationResult(
            mismatch_type="ORDER_EXISTS_UNEXPECTED",
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1_1",
            status=RemediationStatus.EXECUTED,
            action="cancel_all",
        )

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
            _clock=lambda: 1704067200000,
        )
        runner.run()

        metrics = get_reconcile_metrics()
        assert metrics.last_remediation_ts_ms == 1704067200000


# =============================================================================
# ReconcileRunReport Tests
# =============================================================================


class TestReconcileRunReport:
    """Tests for ReconcileRunReport."""

    def test_total_actions(self) -> None:
        """total_actions property calculates correctly."""
        report = ReconcileRunReport(
            ts_start=1000,
            ts_end=2000,
            mismatches_detected=5,
            cancel_results=(
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id="id1",
                    status=RemediationStatus.EXECUTED,
                    action="cancel_all",
                ),
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="ETHUSDT",
                    client_order_id="id2",
                    status=RemediationStatus.PLANNED,
                    action="cancel_all",
                ),
            ),
            flatten_results=(
                RemediationResult(
                    mismatch_type="POSITION_NONZERO_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id=None,
                    status=RemediationStatus.BLOCKED,
                    action="flatten",
                ),
            ),
            skipped_terminal=1,
            skipped_no_action=1,
        )

        assert report.total_actions == 3

    def test_executed_count(self) -> None:
        """executed_count property calculates correctly."""
        report = ReconcileRunReport(
            ts_start=1000,
            ts_end=2000,
            mismatches_detected=3,
            cancel_results=(
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id="id1",
                    status=RemediationStatus.EXECUTED,
                    action="cancel_all",
                ),
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="ETHUSDT",
                    client_order_id="id2",
                    status=RemediationStatus.PLANNED,
                    action="cancel_all",
                ),
            ),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        assert report.executed_count == 1

    def test_planned_count(self) -> None:
        """planned_count property calculates correctly."""
        report = ReconcileRunReport(
            ts_start=1000,
            ts_end=2000,
            mismatches_detected=2,
            cancel_results=(
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id="id1",
                    status=RemediationStatus.PLANNED,
                    action="cancel_all",
                ),
            ),
            flatten_results=(
                RemediationResult(
                    mismatch_type="POSITION_NONZERO_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id=None,
                    status=RemediationStatus.PLANNED,
                    action="flatten",
                ),
            ),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        assert report.planned_count == 2

    def test_blocked_count(self) -> None:
        """blocked_count property calculates correctly."""
        report = ReconcileRunReport(
            ts_start=1000,
            ts_end=2000,
            mismatches_detected=2,
            cancel_results=(
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id="id1",
                    status=RemediationStatus.BLOCKED,
                    action="cancel_all",
                ),
            ),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        assert report.blocked_count == 1

    def test_to_log_extra(self) -> None:
        """to_log_extra returns correct fields."""
        report = ReconcileRunReport(
            ts_start=1000,
            ts_end=2000,
            mismatches_detected=5,
            cancel_results=(
                RemediationResult(
                    mismatch_type="ORDER_EXISTS_UNEXPECTED",
                    symbol="BTCUSDT",
                    client_order_id="id1",
                    status=RemediationStatus.EXECUTED,
                    action="cancel_all",
                ),
            ),
            flatten_results=(),
            skipped_terminal=1,
            skipped_no_action=2,
        )

        extra = report.to_log_extra()

        assert extra["ts_start"] == 1000
        assert extra["ts_end"] == 2000
        assert extra["duration_ms"] == 1000
        assert extra["mismatches_detected"] == 5
        assert extra["cancel_count"] == 1
        assert extra["flatten_count"] == 0
        assert extra["executed_count"] == 1
        assert extra["planned_count"] == 0
        assert extra["blocked_count"] == 0
        assert extra["skipped_terminal"] == 1
        assert extra["skipped_no_action"] == 2


# =============================================================================
# Price Getter Tests
# =============================================================================


class TestPriceGetter:
    """Tests for price_getter usage."""

    def test_price_getter_called_for_flatten(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """price_getter is called for flatten actions."""
        mismatch = _make_mismatch(
            MismatchType.POSITION_NONZERO_UNEXPECTED,
            client_order_id=None,
        )
        mock_engine.reconcile.return_value = [mismatch]

        price_getter = MagicMock(return_value=Decimal("50000"))

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
            price_getter=price_getter,
        )
        runner.run()

        price_getter.assert_called_once_with("BTCUSDT")
        # Verify price was passed to remediate_flatten (positional arg)
        call_args = mock_executor.remediate_flatten.call_args
        # call_args[0] = positional args, call_args[0][1] = current_price
        assert call_args[0][1] == Decimal("50000")

    def test_entry_price_fallback_when_no_price_getter(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Entry price is used when price_getter is None."""
        mismatch = _make_mismatch(
            MismatchType.POSITION_NONZERO_UNEXPECTED,
            client_order_id=None,
        )
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
            price_getter=None,  # No price getter
        )
        runner.run()

        # Verify entry_price (42500) was used (positional arg)
        call_args = mock_executor.remediate_flatten.call_args
        # call_args[0] = positional args, call_args[0][1] = current_price
        assert call_args[0][1] == Decimal("42500")


# =============================================================================
# Executor Reset Tests
# =============================================================================


class TestExecutorReset:
    """Tests for executor.reset_run_counters() being called."""

    def test_reset_run_counters_called(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """reset_run_counters is called at start of run."""
        mock_engine.reconcile.return_value = []

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        runner.run()

        mock_executor.reset_run_counters.assert_called_once()


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_mismatches_returns_empty_report(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Empty mismatches → empty report."""
        mock_engine.reconcile.return_value = []

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert report.mismatches_detected == 0
        assert len(report.cancel_results) == 0
        assert len(report.flatten_results) == 0
        assert report.skipped_terminal == 0
        assert report.skipped_no_action == 0

    def test_mismatch_without_client_order_id_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Cancel mismatch without client_order_id is skipped."""
        mismatch = _make_mismatch(
            MismatchType.ORDER_EXISTS_UNEXPECTED,
            client_order_id=None,  # No client_order_id
        )
        mock_engine.reconcile.return_value = [mismatch]

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        # Should be skipped, not processed
        assert len(report.cancel_results) == 0
        mock_executor.remediate_cancel.assert_not_called()

    def test_order_not_found_in_observed_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Order not found in observed store and no observed dict → skipped."""
        mismatch = Mismatch(
            mismatch_type=MismatchType.ORDER_EXISTS_UNEXPECTED,
            symbol="BTCUSDT",
            client_order_id="grinder_BTCUSDT_1_1_1",
            expected=None,
            observed=None,  # No observed dict
            ts_detected=1000,
            action_plan="would cancel",
        )
        mock_engine.reconcile.return_value = [mismatch]
        mock_observed.get_order.return_value = None  # Not found

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.cancel_results) == 0
        mock_executor.remediate_cancel.assert_not_called()

    def test_position_not_found_skipped(
        self,
        mock_engine: MagicMock,
        mock_executor: MagicMock,
        mock_observed: MagicMock,
    ) -> None:
        """Position not found in observed store and no observed dict → skipped."""
        mismatch = Mismatch(
            mismatch_type=MismatchType.POSITION_NONZERO_UNEXPECTED,
            symbol="BTCUSDT",
            client_order_id=None,
            expected=None,
            observed=None,  # No observed dict
            ts_detected=1000,
            action_plan="would flatten",
        )
        mock_engine.reconcile.return_value = [mismatch]
        mock_observed.get_position.return_value = None  # Not found

        runner = ReconcileRunner(
            engine=mock_engine,
            executor=mock_executor,
            observed=mock_observed,
        )
        report = runner.run()

        assert len(report.flatten_results) == 0
        mock_executor.remediate_flatten.assert_not_called()
