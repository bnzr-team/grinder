"""Tests for ReconcileEngine."""

from decimal import Decimal

import pytest

from grinder.core import OrderSide, OrderState
from grinder.execution.futures_events import FuturesOrderEvent, FuturesPositionEvent
from grinder.reconcile.config import ReconcileConfig
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.metrics import ReconcileMetrics
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.types import ExpectedOrder, ExpectedPosition, MismatchType


class TestReconcileEngine:
    """Tests for ReconcileEngine."""

    @pytest.fixture
    def config(self) -> ReconcileConfig:
        """Create test config."""
        return ReconcileConfig(
            order_grace_period_ms=5000,
            enabled=True,
        )

    @pytest.fixture
    def expected_store(self) -> ExpectedStateStore:
        """Create expected state store."""
        return ExpectedStateStore(_clock=lambda: 10000000)

    @pytest.fixture
    def observed_store(self) -> ObservedStateStore:
        """Create observed state store."""
        return ObservedStateStore()

    @pytest.fixture
    def metrics(self) -> ReconcileMetrics:
        """Create metrics."""
        return ReconcileMetrics()

    @pytest.fixture
    def engine(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> ReconcileEngine:
        """Create engine with test clock."""
        return ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )


class TestOrderMissingOnExchange(TestReconcileEngine):
    """Tests for ORDER_MISSING_ON_EXCHANGE detection."""

    def test_detects_order_missing_after_grace_period(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should detect order missing after grace period."""
        # Expected order created 10s ago (> 5s grace)
        expected_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=10000000 - 10000,  # 10 seconds ago
        )
        expected_store.record_order(expected_order)

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.ORDER_MISSING_ON_EXCHANGE
        assert mismatches[0].client_order_id == "grinder_BTCUSDT_1_1000000_1"

    def test_no_mismatch_within_grace_period(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should not detect missing order within grace period."""
        # Expected order created 3s ago (< 5s grace)
        expected_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=10000000 - 3000,  # 3 seconds ago
        )
        expected_store.record_order(expected_order)

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 0

    def test_no_mismatch_when_order_observed(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should not detect missing order when it's observed."""
        expected_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=10000000 - 10000,
        )
        expected_store.record_order(expected_order)

        # Order is observed
        observed_store.update_from_order_event(
            FuturesOrderEvent(
                ts=10000000 - 5000,
                symbol="BTCUSDT",
                order_id=12345678,
                client_order_id="grinder_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 0


class TestOrderExistsUnexpected(TestReconcileEngine):
    """Tests for ORDER_EXISTS_UNEXPECTED detection."""

    def test_detects_unexpected_grinder_order(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should detect unexpected grinder_ order (v1 format with strategy_id)."""
        # No expected orders, but one observed (using v1 format: grinder_{strategy}_{symbol}_{level}_{ts}_{seq})
        observed_store.update_from_order_event(
            FuturesOrderEvent(
                ts=10000000,
                symbol="BTCUSDT",
                order_id=12345678,
                client_order_id="grinder_default_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.ORDER_EXISTS_UNEXPECTED
        assert mismatches[0].client_order_id == "grinder_default_BTCUSDT_1_1000000_1"

    def test_ignores_non_grinder_orders(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should ignore orders without grinder_ prefix."""
        # Observed order with non-grinder prefix
        observed_store.update_from_order_event(
            FuturesOrderEvent(
                ts=10000000,
                symbol="BTCUSDT",
                order_id=12345678,
                client_order_id="manual_order_123",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 0

    def test_no_mismatch_when_order_expected(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should not detect unexpected when order is expected."""
        expected_order = ExpectedOrder(
            client_order_id="grinder_BTCUSDT_1_1000000_1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type="LIMIT",
            price=Decimal("42500.00"),
            orig_qty=Decimal("0.010"),
            ts_created=10000000 - 3000,  # Within grace
        )
        expected_store.record_order(expected_order)

        observed_store.update_from_order_event(
            FuturesOrderEvent(
                ts=10000000,
                symbol="BTCUSDT",
                order_id=12345678,
                client_order_id="grinder_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 0


class TestPositionNonzeroUnexpected(TestReconcileEngine):
    """Tests for POSITION_NONZERO_UNEXPECTED detection."""

    def test_detects_nonzero_position_when_expected_zero(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should detect nonzero position when expected is zero."""
        # Expected zero position
        expected_store.set_position(
            ExpectedPosition(
                symbol="BTCUSDT",
                expected_position_amt=Decimal("0"),
                ts_updated=10000000,
            )
        )

        # Observed nonzero position
        observed_store.update_from_position_event(
            FuturesPositionEvent(
                ts=10000000,
                symbol="BTCUSDT",
                position_amt=Decimal("0.010"),
                entry_price=Decimal("42500.00"),
                unrealized_pnl=Decimal("50.00"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.POSITION_NONZERO_UNEXPECTED
        assert mismatches[0].symbol == "BTCUSDT"
        assert "0.010" in mismatches[0].action_plan

    def test_no_mismatch_when_position_is_zero(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should not detect mismatch when position is zero."""
        expected_store.set_position(
            ExpectedPosition(symbol="BTCUSDT", expected_position_amt=Decimal("0"))
        )

        observed_store.update_from_position_event(
            FuturesPositionEvent(
                ts=10000000,
                symbol="BTCUSDT",
                position_amt=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 0


class TestReconcileEngineMetrics(TestReconcileEngine):
    """Tests for metrics updates."""

    def test_updates_mismatch_metrics(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should update mismatch metrics."""
        # Set up mismatches
        expected_store.set_position(
            ExpectedPosition(symbol="BTCUSDT", expected_position_amt=Decimal("0"))
        )
        observed_store.update_from_position_event(
            FuturesPositionEvent(
                ts=10000000,
                symbol="BTCUSDT",
                position_amt=Decimal("0.010"),
                entry_price=Decimal("42500.00"),
                unrealized_pnl=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        engine.reconcile()

        assert metrics.mismatch_counts.get("POSITION_NONZERO_UNEXPECTED", 0) == 1

    def test_updates_reconcile_run_count(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should update reconcile run count."""
        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        engine.reconcile()
        engine.reconcile()
        engine.reconcile()

        assert metrics.reconcile_runs == 3

    def test_updates_snapshot_age_metric(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should update snapshot age metric."""
        # Set snapshot timestamp
        observed_store.update_from_rest_orders([], ts=10000000 - 5000)

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        engine.reconcile()

        assert metrics.last_snapshot_age_ms == 5000


class TestReconcileEngineDisabled(TestReconcileEngine):
    """Tests for disabled reconciliation."""

    def test_returns_empty_when_disabled(
        self,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should return empty list when disabled."""
        config = ReconcileConfig(enabled=False)

        # Set up a mismatch that would normally be detected
        observed_store.update_from_order_event(
            FuturesOrderEvent(
                ts=10000000,
                symbol="BTCUSDT",
                order_id=12345678,
                client_order_id="grinder_BTCUSDT_1_1000000_1",
                side=OrderSide.BUY,
                status=OrderState.OPEN,
                price=Decimal("42500.00"),
                qty=Decimal("0.010"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 0


class TestReconcileEngineMultipleMismatches(TestReconcileEngine):
    """Tests for multiple mismatches."""

    def test_detects_multiple_mismatches(
        self,
        config: ReconcileConfig,
        expected_store: ExpectedStateStore,
        observed_store: ObservedStateStore,
        metrics: ReconcileMetrics,
    ) -> None:
        """Should detect multiple mismatches in one run."""
        # Missing order (created 10s ago) - v1 format with strategy_id
        expected_store.record_order(
            ExpectedOrder(
                client_order_id="grinder_default_BTCUSDT_1_1000000_1",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type="LIMIT",
                price=Decimal("42500.00"),
                orig_qty=Decimal("0.010"),
                ts_created=10000000 - 10000,
            )
        )

        # Unexpected order - v1 format with strategy_id
        observed_store.update_from_order_event(
            FuturesOrderEvent(
                ts=10000000,
                symbol="ETHUSDT",
                order_id=99999999,
                client_order_id="grinder_default_ETHUSDT_1_1000000_1",
                side=OrderSide.SELL,
                status=OrderState.OPEN,
                price=Decimal("3000.00"),
                qty=Decimal("0.1"),
                executed_qty=Decimal("0"),
                avg_price=Decimal("0"),
            )
        )

        # Nonzero position
        expected_store.set_position(
            ExpectedPosition(symbol="BTCUSDT", expected_position_amt=Decimal("0"))
        )
        observed_store.update_from_position_event(
            FuturesPositionEvent(
                ts=10000000,
                symbol="BTCUSDT",
                position_amt=Decimal("0.020"),
                entry_price=Decimal("42500.00"),
                unrealized_pnl=Decimal("100.00"),
            )
        )

        engine = ReconcileEngine(
            config=config,
            expected=expected_store,
            observed=observed_store,
            metrics=metrics,
            _clock=lambda: 10000000,
        )

        mismatches = engine.reconcile()

        assert len(mismatches) == 3

        mismatch_types = {m.mismatch_type for m in mismatches}
        assert MismatchType.ORDER_MISSING_ON_EXCHANGE in mismatch_types
        assert MismatchType.ORDER_EXISTS_UNEXPECTED in mismatch_types
        assert MismatchType.POSITION_NONZERO_UNEXPECTED in mismatch_types
