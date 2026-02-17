"""Tests for data quality gating in remediation (Launch-03 PR3).

Tests verify:
- dq_blocking=False → DQ gate ignored, no blocking.
- dq_blocking=True + stale → blocked with reason data_quality_stale.
- dq_blocking=True + gap → blocked with reason data_quality_gap.
- dq_blocking=True + outlier → blocked with reason data_quality_outlier.
- executed_total does NOT increase when DQ-blocked.
- Priority ordering: stale > gap > outlier.
- No forbidden labels (no symbol=).
- LiveFeed verdict integration (verdict stored + accessible).
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from grinder.contracts import Snapshot
from grinder.core import OrderSide, OrderState
from grinder.data.quality import DataQualityConfig
from grinder.data.quality_engine import DataQualityEngine, DataQualityVerdict
from grinder.ha.role import HARole, reset_ha_state, set_ha_state
from grinder.live.feed import LiveFeed, LiveFeedConfig
from grinder.reconcile.config import ReconcileConfig, RemediationAction, RemediationMode
from grinder.reconcile.metrics import get_reconcile_metrics, reset_reconcile_metrics
from grinder.reconcile.remediation import (
    RemediationBlockReason,
    RemediationExecutor,
    RemediationStatus,
)
from grinder.reconcile.types import ObservedOrder, ObservedPosition

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_reconcile_metrics()


@pytest.fixture(autouse=True)
def _reset_ha() -> Generator[None, None, None]:
    set_ha_state(role=HARole.ACTIVE)
    yield
    reset_ha_state()


@pytest.fixture(autouse=True)
def _clean_env() -> Generator[None, None, None]:
    os.environ.pop("ALLOW_MAINNET_TRADE", None)
    yield
    os.environ.pop("ALLOW_MAINNET_TRADE", None)


@pytest.fixture
def mock_port() -> MagicMock:
    port = MagicMock()
    port.cancel_order.return_value = True
    port.place_market_order.return_value = "grinder_default_BTCUSDT_cleanup_123_1"
    return port


@pytest.fixture
def observed_order() -> ObservedOrder:
    return ObservedOrder(
        client_order_id="grinder_default_BTCUSDT_1_1704067200000_1",
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
def observed_position() -> ObservedPosition:
    return ObservedPosition(
        symbol="BTCUSDT",
        position_amt=Decimal("0.010"),
        entry_price=Decimal("42500.00"),
        unrealized_pnl=Decimal("10.00"),
        ts_observed=1704067200000,
    )


def _make_executor(
    mock_port: MagicMock,
    *,
    dq_blocking: bool = False,
    dq_verdict_fn: Callable[[], DataQualityVerdict | None] | None = None,
    remediation_mode: RemediationMode = RemediationMode.EXECUTE_CANCEL_ALL,
) -> RemediationExecutor:
    """Build a fully-armed executor for DQ gating tests."""
    os.environ["ALLOW_MAINNET_TRADE"] = "1"
    config = ReconcileConfig(
        action=RemediationAction.CANCEL_ALL,
        dry_run=False,
        allow_active_remediation=True,
        require_whitelist=False,
        remediation_mode=remediation_mode,
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
        max_calls_per_day=1000,
        max_notional_per_day=Decimal("1000000"),
    )
    return RemediationExecutor(
        config=config,
        port=mock_port,
        armed=True,
        dq_blocking=dq_blocking,
        dq_verdict_fn=dq_verdict_fn,
    )


# ---------------------------------------------------------------------------
# DataQualityVerdict unit tests
# ---------------------------------------------------------------------------


class TestDataQualityVerdict:
    """Tests for the DataQualityVerdict dataclass."""

    def test_ok_when_all_clear(self) -> None:
        v = DataQualityVerdict()
        assert v.is_ok is True

    def test_not_ok_when_stale(self) -> None:
        v = DataQualityVerdict(stale=True)
        assert v.is_ok is False

    def test_not_ok_when_gap(self) -> None:
        v = DataQualityVerdict(gap_bucket="500")
        assert v.is_ok is False

    def test_not_ok_when_outlier(self) -> None:
        v = DataQualityVerdict(outlier_kind="price")
        assert v.is_ok is False

    def test_frozen(self) -> None:
        v = DataQualityVerdict()
        with pytest.raises(AttributeError):
            v.stale = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# observe_tick returns verdict
# ---------------------------------------------------------------------------


class TestEngineReturnsVerdict:
    """Tests that DataQualityEngine.observe_tick returns a DataQualityVerdict."""

    def test_clean_tick_returns_ok_verdict(self) -> None:
        dq = DataQualityEngine()
        v = dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        assert isinstance(v, DataQualityVerdict)
        assert v.is_ok is True

    def test_gap_tick_returns_verdict_with_bucket(self) -> None:
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000))
        dq = DataQualityEngine(cfg)
        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        v = dq.observe_tick(stream="test", ts_ms=2000, price=100.0)
        assert v.gap_bucket == "500"
        assert v.is_ok is False

    def test_outlier_tick_returns_verdict_with_kind(self) -> None:
        cfg = DataQualityConfig(price_jump_max_bps=100)
        dq = DataQualityEngine(cfg)
        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        v = dq.observe_tick(stream="test", ts_ms=2000, price=120.0)
        assert v.outlier_kind == "price"
        assert v.is_ok is False

    def test_stale_tick_returns_verdict_stale(self) -> None:
        cfg = DataQualityConfig(stale_book_ticker_ms=1000)
        dq = DataQualityEngine(cfg)
        dq.observe_tick(stream="test", ts_ms=1000, price=100.0, now_ms=1000)
        v = dq.observe_tick(stream="test", ts_ms=2000, price=100.0, now_ms=5000)
        assert v.stale is True
        assert v.is_ok is False


# ---------------------------------------------------------------------------
# DQ gating in can_execute
# ---------------------------------------------------------------------------


class TestDqGating:
    """Tests for DQ-based blocking in RemediationExecutor.can_execute."""

    def test_dq_blocking_false_allows_even_bad_verdict(self, mock_port: MagicMock) -> None:
        """dq_blocking=False → DQ gate is skipped entirely."""
        bad_verdict = DataQualityVerdict(stale=True)
        executor = _make_executor(
            mock_port,
            dq_blocking=False,
            dq_verdict_fn=lambda: bad_verdict,
        )
        can, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is True
        assert reason is None

    def test_dq_blocking_true_no_fn_allows(self, mock_port: MagicMock) -> None:
        """dq_blocking=True but no verdict_fn → gate skipped (safe default)."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=None,
        )
        can, _reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is True

    def test_dq_blocking_true_ok_verdict_allows(self, mock_port: MagicMock) -> None:
        """dq_blocking=True + clean verdict → allowed."""
        ok_verdict = DataQualityVerdict()
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: ok_verdict,
        )
        can, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is True
        assert reason is None

    def test_dq_blocking_true_none_verdict_allows(self, mock_port: MagicMock) -> None:
        """dq_blocking=True + None verdict (no tick yet) → allowed."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: None,
        )
        can, _reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is True

    def test_stale_blocks_with_correct_reason(self, mock_port: MagicMock) -> None:
        """Stale verdict → DATA_QUALITY_STALE."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(stale=True),
        )
        can, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is False
        assert reason == RemediationBlockReason.DATA_QUALITY_STALE

    def test_gap_blocks_with_correct_reason(self, mock_port: MagicMock) -> None:
        """Gap verdict → DATA_QUALITY_GAP."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(gap_bucket="2000"),
        )
        can, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is False
        assert reason == RemediationBlockReason.DATA_QUALITY_GAP

    def test_outlier_blocks_with_correct_reason(self, mock_port: MagicMock) -> None:
        """Outlier verdict → DATA_QUALITY_OUTLIER."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(outlier_kind="price"),
        )
        can, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert can is False
        assert reason == RemediationBlockReason.DATA_QUALITY_OUTLIER

    def test_priority_stale_over_gap(self, mock_port: MagicMock) -> None:
        """When multiple issues, stale wins over gap."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(stale=True, gap_bucket="500"),
        )
        _, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert reason == RemediationBlockReason.DATA_QUALITY_STALE

    def test_priority_gap_over_outlier(self, mock_port: MagicMock) -> None:
        """When gap + outlier, gap wins."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(gap_bucket="500", outlier_kind="price"),
        )
        _, reason = executor.can_execute("BTCUSDT", is_cancel=True)
        assert reason == RemediationBlockReason.DATA_QUALITY_GAP


# ---------------------------------------------------------------------------
# End-to-end: remediate_cancel / remediate_flatten with DQ blocking
# ---------------------------------------------------------------------------


class TestDqBlockingRemediation:
    """Verify DQ blocking integrates with full remediation flow."""

    def test_cancel_blocked_by_dq_increments_blocked_metric(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """DQ-blocked cancel increments action_blocked_total."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(stale=True),
        )
        result = executor.remediate_cancel(observed_order)
        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.DATA_QUALITY_STALE

        metrics = get_reconcile_metrics()
        assert metrics.action_blocked_counts.get("data_quality_stale", 0) == 1

    def test_cancel_blocked_by_dq_does_not_execute(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """DQ-blocked cancel does NOT call port.cancel_order."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(gap_bucket="2000"),
        )
        executor.remediate_cancel(observed_order)
        mock_port.cancel_order.assert_not_called()

    def test_cancel_blocked_executed_total_unchanged(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """DQ-blocked cancel: executed_total stays zero."""
        executor = _make_executor(
            mock_port,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(outlier_kind="price"),
        )
        executor.remediate_cancel(observed_order)
        metrics = get_reconcile_metrics()
        assert sum(metrics.action_executed_counts.values()) == 0

    def test_flatten_blocked_by_dq(
        self, mock_port: MagicMock, observed_position: ObservedPosition
    ) -> None:
        """DQ-blocked flatten increments blocked metric."""
        config = ReconcileConfig(
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,
            allow_active_remediation=True,
            require_whitelist=False,
            remediation_mode=RemediationMode.EXECUTE_FLATTEN,
            max_calls_per_run=100,
            max_notional_per_run=Decimal("100000"),
            max_calls_per_day=1000,
            max_notional_per_day=Decimal("1000000"),
        )
        os.environ["ALLOW_MAINNET_TRADE"] = "1"
        executor = RemediationExecutor(
            config=config,
            port=mock_port,
            armed=True,
            dq_blocking=True,
            dq_verdict_fn=lambda: DataQualityVerdict(stale=True),
        )
        result = executor.remediate_flatten(observed_position, Decimal("42500.00"))
        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.DATA_QUALITY_STALE
        mock_port.place_market_order.assert_not_called()

    def test_dq_blocking_false_allows_cancel(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """dq_blocking=False → cancel proceeds even with bad verdict."""
        executor = _make_executor(
            mock_port,
            dq_blocking=False,
            dq_verdict_fn=lambda: DataQualityVerdict(stale=True),
        )
        result = executor.remediate_cancel(observed_order)
        assert result.status == RemediationStatus.EXECUTED
        mock_port.cancel_order.assert_called_once()


# ---------------------------------------------------------------------------
# Label safety
# ---------------------------------------------------------------------------


class TestDqBlockReasonLabels:
    """Verify DQ block reason strings are safe for Prometheus labels."""

    def test_reason_values_are_lowercase_snake_case(self) -> None:
        """All 3 DQ reasons must be lowercase snake_case (metric-safe)."""
        for reason in (
            RemediationBlockReason.DATA_QUALITY_STALE,
            RemediationBlockReason.DATA_QUALITY_GAP,
            RemediationBlockReason.DATA_QUALITY_OUTLIER,
        ):
            assert reason.value == reason.value.lower()
            assert " " not in reason.value
            assert reason.value.startswith("data_quality_")

    def test_no_symbol_in_reason_values(self) -> None:
        """DQ reason values must not contain 'symbol'."""
        for reason in (
            RemediationBlockReason.DATA_QUALITY_STALE,
            RemediationBlockReason.DATA_QUALITY_GAP,
            RemediationBlockReason.DATA_QUALITY_OUTLIER,
        ):
            assert "symbol" not in reason.value


# ---------------------------------------------------------------------------
# LiveFeed verdict integration
# ---------------------------------------------------------------------------


class TestLiveFeedVerdict:
    """Verify LiveFeed stores and exposes the DQ verdict."""

    def test_verdict_none_when_dq_disabled(self) -> None:
        """dq_enabled=False → latest verdict is None."""
        feed = LiveFeed(
            LiveFeedConfig(symbols=["BTCUSDT"], dq_enabled=False),
            clock=lambda: 1.0,
        )
        assert feed.latest_data_quality_verdict() is None

    def test_verdict_none_before_first_tick(self) -> None:
        """dq_enabled=True → None until first tick processed."""
        feed = LiveFeed(
            LiveFeedConfig(symbols=["BTCUSDT"], dq_enabled=True),
            clock=lambda: 1.0,
        )
        assert feed.latest_data_quality_verdict() is None

    def test_verdict_populated_after_tick(self) -> None:
        """After processing a snapshot, verdict is available."""
        feed = LiveFeed(
            LiveFeedConfig(symbols=["BTCUSDT"], dq_enabled=True),
            clock=lambda: 1.0,
        )
        snap = Snapshot(
            ts=1_000_000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            ask_price=Decimal("50001"),
            bid_qty=Decimal("1.0"),
            ask_qty=Decimal("1.0"),
            last_price=Decimal("50000"),
            last_qty=Decimal("0.5"),
        )
        feed.process_snapshot_sync(snap)
        verdict = feed.latest_data_quality_verdict()
        assert verdict is not None
        assert isinstance(verdict, DataQualityVerdict)
        assert verdict.is_ok is True

    def test_dq_blocking_config_default_false(self) -> None:
        """dq_blocking defaults to False in LiveFeedConfig."""
        cfg = LiveFeedConfig()
        assert cfg.dq_blocking is False
