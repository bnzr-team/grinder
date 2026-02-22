"""Tests for fill probability circuit breaker (Track C, PR-C8).

Covers:
- Shadow-safe: record() is no-op when enforce=False.
- Trip: block rate exceeds max → is_tripped() returns True.
- Reset: window rolls over → is_tripped() returns False.
- Fail-open: any internal error → is_tripped() returns False.
- Trip counter: increments only on first trip per episode.
- Integration: engine bypasses gate when CB is tripped.
- Metrics: cb_trips_total counter in SorMetrics.
- Contract: REQUIRED_METRICS_PATTERNS includes CB metric.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.fill_prob_gate import (
    FillProbCircuitBreaker,
    FillProbVerdict,
)
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import (
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineV0,
)
from grinder.ml.fill_model_v0 import FillModelV0
from grinder.observability.metrics_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --- Helpers ---------------------------------------------------------------


def _make_snapshot() -> Snapshot:
    return Snapshot(
        ts=1000000,
        symbol="BTCUSDT",
        bid_price=Decimal("50000.00"),
        ask_price=Decimal("50001.00"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal("50000.00"),
        last_qty=Decimal("0.5"),
    )


def _place_action() -> ExecutionAction:
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("49000.00"),
        quantity=Decimal("0.01"),
        level_id=1,
        reason="GRID_ENTRY",
    )


def _make_model(tmp_path: Path, prob_bps: int = 1000) -> FillModelV0:
    """Create a model with known low-prob bin for long|1|1|0."""
    model = FillModelV0(bins={"long|1|1|0": prob_bps}, global_prior_bps=5000, n_train_rows=100)
    model.save(tmp_path / "model", force=True, created_at_utc="2025-01-01T00:00:00Z")
    return model


# --- Tests: Circuit breaker unit ------------------------------------------


class TestCircuitBreakerUnit:
    """CB-001..CB-008: circuit breaker logic."""

    def test_shadow_mode_noop(self) -> None:
        """CB-001: record() is no-op when enforce=False."""
        cb = FillProbCircuitBreaker(window_seconds=60, max_block_rate_pct=50)
        # Record 10 blocks in shadow mode
        for _ in range(10):
            cb.record(FillProbVerdict.BLOCK, enforce=False)
        assert not cb.is_tripped()
        assert cb.trip_count == 0

    def test_trip_on_high_block_rate(self) -> None:
        """CB-002: trips when block rate exceeds max_block_rate_pct."""
        cb = FillProbCircuitBreaker(window_seconds=300, max_block_rate_pct=50)
        # 3 blocks out of 4 events = 75% > 50%
        cb.record(FillProbVerdict.ALLOW, enforce=True)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        assert cb.is_tripped()
        assert cb.trip_count == 1

    def test_no_trip_below_threshold(self) -> None:
        """CB-003: does not trip when block rate is below threshold."""
        cb = FillProbCircuitBreaker(window_seconds=300, max_block_rate_pct=50)
        # 1 block out of 4 events = 25% < 50%
        cb.record(FillProbVerdict.ALLOW, enforce=True)
        cb.record(FillProbVerdict.ALLOW, enforce=True)
        cb.record(FillProbVerdict.ALLOW, enforce=True)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        assert not cb.is_tripped()
        assert cb.trip_count == 0

    def test_reset_after_window_expires(self) -> None:
        """CB-004: trip resets when window rolls over."""
        cb = FillProbCircuitBreaker(window_seconds=10, max_block_rate_pct=50)
        # Trip the breaker
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        assert cb.is_tripped()

        # Fast-forward time past window
        with patch("grinder.execution.fill_prob_gate.time") as mock_time:
            mock_time.monotonic.return_value = 1e9  # far future
            assert not cb.is_tripped()

    def test_trip_counter_increments_once_per_episode(self) -> None:
        """CB-005: trip_count increments only on first trip, not on subsequent records."""
        cb = FillProbCircuitBreaker(window_seconds=300, max_block_rate_pct=50)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        assert cb.trip_count == 1
        # More blocks while tripped — still 1
        cb.record(FillProbVerdict.BLOCK, enforce=True)
        assert cb.trip_count == 1

    def test_fail_open_on_error(self) -> None:
        """CB-006: is_tripped() returns False on internal error."""
        cb = FillProbCircuitBreaker(window_seconds=300, max_block_rate_pct=50)
        cb._tripped = True
        # Corrupt internal state to force error
        cb._events = None  # type: ignore[assignment]
        assert not cb.is_tripped()

    def test_empty_window_not_tripped(self) -> None:
        """CB-007: empty event deque → not tripped."""
        cb = FillProbCircuitBreaker(window_seconds=300, max_block_rate_pct=50)
        assert not cb.is_tripped()
        assert cb.trip_count == 0

    def test_all_allows_not_tripped(self) -> None:
        """CB-008: 100% allow rate → not tripped."""
        cb = FillProbCircuitBreaker(window_seconds=300, max_block_rate_pct=50)
        for _ in range(20):
            cb.record(FillProbVerdict.ALLOW, enforce=True)
        assert not cb.is_tripped()
        assert cb.trip_count == 0


# --- Tests: Engine integration -------------------------------------------


class TestEngineCircuitBreaker:
    """ECB-001..ECB-002: engine wiring for circuit breaker."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_cb_tripped_bypasses_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ECB-001: when CB is tripped, gate is bypassed → order executes."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "5000")

        model = _make_model(tmp_path, prob_bps=1000)  # would block
        action = _place_action()

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=MagicMock(
                process_snapshot=MagicMock(return_value=MagicMock(actions=[action]))
            ),
            exchange_port=port,
            config=LiveEngineConfig(
                armed=True,
                mode=SafeMode.LIVE_TRADE,
                kill_switch_active=False,
                symbol_whitelist=[],
            ),
            fill_model=model,
        )

        # Force CB to tripped state by recording enough blocks
        for _ in range(5):
            engine._fill_prob_cb.record(FillProbVerdict.BLOCK, enforce=True)
        assert engine._fill_prob_cb.is_tripped()

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        # Should be EXECUTED because CB bypassed the gate
        assert la.status == LiveActionStatus.EXECUTED
        port.place_order.assert_called_once()

    def test_cb_not_tripped_gate_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ECB-002: when CB is NOT tripped, gate blocks as normal."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "5000")

        model = _make_model(tmp_path, prob_bps=1000)  # will block
        action = _place_action()

        port = MagicMock()
        engine = LiveEngineV0(
            paper_engine=MagicMock(
                process_snapshot=MagicMock(return_value=MagicMock(actions=[action]))
            ),
            exchange_port=port,
            config=LiveEngineConfig(
                armed=True,
                mode=SafeMode.LIVE_TRADE,
                kill_switch_active=False,
                symbol_whitelist=[],
            ),
            fill_model=model,
        )

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        assert la.status == LiveActionStatus.BLOCKED


# --- Tests: Metrics -------------------------------------------------------


class TestCbMetrics:
    """CBM-001..CBM-004: circuit breaker metrics."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_cb_trip_counter(self) -> None:
        """CBM-001: record_cb_trip increments counter."""
        metrics = get_sor_metrics()
        assert metrics.fill_prob_cb_trips == 0
        metrics.record_cb_trip()
        metrics.record_cb_trip()
        assert metrics.fill_prob_cb_trips == 2

    def test_prometheus_lines_include_cb_trips(self) -> None:
        """CBM-002: to_prometheus_lines() includes CB trips metric."""
        metrics = get_sor_metrics()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_router_fill_prob_cb_trips_total 0" in text

    def test_prometheus_lines_cb_trips_after_increment(self) -> None:
        """CBM-003: to_prometheus_lines() shows incremented CB trips."""
        metrics = get_sor_metrics()
        metrics.record_cb_trip()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_router_fill_prob_cb_trips_total 1" in text

    def test_prometheus_help_and_type(self) -> None:
        """CBM-004: Prometheus output includes HELP and TYPE for CB trips."""
        metrics = get_sor_metrics()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "# HELP grinder_router_fill_prob_cb_trips_total" in text
        assert "# TYPE grinder_router_fill_prob_cb_trips_total counter" in text


# --- Tests: Contract ------------------------------------------------------


class TestCbContract:
    """CBC-001..CBC-002: metrics contract compliance for CB metric."""

    def test_required_patterns_present(self) -> None:
        """CBC-001: REQUIRED_METRICS_PATTERNS includes CB metric patterns."""
        expected = [
            "# HELP grinder_router_fill_prob_cb_trips_total",
            "# TYPE grinder_router_fill_prob_cb_trips_total",
            "grinder_router_fill_prob_cb_trips_total",
        ]
        for pattern in expected:
            assert pattern in REQUIRED_METRICS_PATTERNS, f"Missing pattern: {pattern!r}"

    def test_no_forbidden_labels(self) -> None:
        """CBC-002: CB metric has no FORBIDDEN_METRIC_LABELS."""
        metrics = get_sor_metrics()
        metrics.record_cb_trip()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in text, f"Forbidden label {label!r} found"

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()
