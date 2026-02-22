"""Tests for fill probability gate (Track C, PR-C5).

Covers:
- Pure gate function: ALLOW / BLOCK / SHADOW verdicts.
- Fail-open: model=None → ALLOW.
- Shadow mode: enforce=False → SHADOW (never blocks).
- Enforce mode: prob >= threshold → ALLOW, prob < threshold → BLOCK.
- Engine integration: Gate 7 wiring in LiveEngineV0.
- Metrics: fill_prob_blocks_total counter, fill_prob_enforce_enabled gauge.
- Contract: new metrics in REQUIRED_METRICS_PATTERNS, no FORBIDDEN_METRIC_LABELS.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.fill_prob_gate import (
    FillProbVerdict,
    check_fill_prob,
)
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live import (
    BlockReason,
    LiveActionStatus,
    LiveEngineConfig,
    LiveEngineV0,
)
from grinder.ml.fill_model_loader import extract_online_features
from grinder.ml.fill_model_v0 import FillModelFeaturesV0, FillModelV0
from grinder.observability.metrics_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


def _make_model(tmp_path: Path, *, bins: dict[str, int] | None = None) -> FillModelV0:
    """Create a FillModelV0 with given bins (default: low-prob bin)."""
    if bins is None:
        bins = {"long|0|1|0": 1000, "short|1|2|1": 8000}
    model = FillModelV0(bins=bins, global_prior_bps=5000, n_train_rows=100)
    model_dir = tmp_path / "fill_model_v0"
    model.save(model_dir, force=True, created_at_utc="2025-01-01T00:00:00Z")
    return model


def _make_features(direction: str = "long", notional: float = 50.0) -> FillModelFeaturesV0:
    """Create FillModelFeaturesV0 via extract_online_features."""
    return extract_online_features(direction=direction, notional=notional)


def _make_snapshot(bid: str = "50000.00", ask: str = "50001.00", ts: int = 1000000) -> Snapshot:
    """Create a Snapshot with given bid/ask."""
    return Snapshot(
        ts=ts,
        symbol="BTCUSDT",
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal(bid),
        last_qty=Decimal("0.5"),
    )


def _make_paper_engine(actions: list[ExecutionAction]) -> MagicMock:
    """Create a mock PaperEngine returning given actions."""
    engine = MagicMock()
    engine.process_snapshot.return_value = MagicMock(actions=actions)
    return engine


def _place_action(
    price: str = "49000.00", qty: str = "0.01", side: OrderSide = OrderSide.BUY
) -> ExecutionAction:
    """Create a PLACE action."""
    return ExecutionAction(
        action_type=ActionType.PLACE,
        symbol="BTCUSDT",
        side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        level_id=1,
        reason="GRID_ENTRY",
    )


def _cancel_action() -> ExecutionAction:
    """Create a CANCEL action."""
    return ExecutionAction(
        action_type=ActionType.CANCEL,
        order_id="ORDER_123",
        symbol="BTCUSDT",
        reason="GRID_EXIT",
    )


def _live_config() -> LiveEngineConfig:
    """Create a LiveEngineConfig with all gates open."""
    return LiveEngineConfig(
        armed=True,
        mode=SafeMode.LIVE_TRADE,
        kill_switch_active=False,
        symbol_whitelist=[],
    )


# --- Tests: Pure gate function ---------------------------------------------


class TestCheckFillProb:
    """G-001..G-006: pure gate function tests."""

    def test_model_none_returns_allow(self) -> None:
        """G-001: model=None → ALLOW (fail-open), prob_bps=0."""
        features = _make_features()
        result = check_fill_prob(model=None, features=features, threshold_bps=2500)
        assert result.verdict == FillProbVerdict.ALLOW
        assert result.prob_bps == 0
        assert result.enforce is False

    def test_model_none_enforce_true_still_allow(self) -> None:
        """G-002: model=None + enforce=True → still ALLOW (fail-open)."""
        features = _make_features()
        result = check_fill_prob(model=None, features=features, threshold_bps=2500, enforce=True)
        assert result.verdict == FillProbVerdict.ALLOW
        assert result.prob_bps == 0
        assert result.enforce is True

    def test_shadow_mode_returns_shadow(self, tmp_path: Path) -> None:
        """G-003: enforce=False → SHADOW (never blocks, prediction computed)."""
        model = _make_model(tmp_path)
        features = _make_features()
        result = check_fill_prob(model=model, features=features, threshold_bps=2500, enforce=False)
        assert result.verdict == FillProbVerdict.SHADOW
        assert result.prob_bps > 0
        assert result.enforce is False

    def test_enforce_above_threshold_returns_allow(self, tmp_path: Path) -> None:
        """G-004: enforce=True + prob >= threshold → ALLOW."""
        # Model with high-prob bin: long|0|1|0 = 9000 bps
        model = _make_model(tmp_path, bins={"long|0|1|0": 9000})
        features = _make_features(direction="long", notional=50.0)
        result = check_fill_prob(model=model, features=features, threshold_bps=2500, enforce=True)
        assert result.verdict == FillProbVerdict.ALLOW
        assert result.prob_bps == 9000
        assert result.enforce is True

    def test_enforce_below_threshold_returns_block(self, tmp_path: Path) -> None:
        """G-005: enforce=True + prob < threshold → BLOCK."""
        # Model with low-prob bin: long|0|1|0 = 1000 bps
        model = _make_model(tmp_path, bins={"long|0|1|0": 1000})
        features = _make_features(direction="long", notional=50.0)
        result = check_fill_prob(model=model, features=features, threshold_bps=2500, enforce=True)
        assert result.verdict == FillProbVerdict.BLOCK
        assert result.prob_bps == 1000
        assert result.threshold_bps == 2500
        assert result.enforce is True

    def test_enforce_at_exact_threshold_returns_allow(self, tmp_path: Path) -> None:
        """G-006: enforce=True + prob == threshold → ALLOW (>= semantics)."""
        model = _make_model(tmp_path, bins={"long|0|1|0": 2500})
        features = _make_features(direction="long", notional=50.0)
        result = check_fill_prob(model=model, features=features, threshold_bps=2500, enforce=True)
        assert result.verdict == FillProbVerdict.ALLOW
        assert result.prob_bps == 2500

    def test_result_is_frozen(self, tmp_path: Path) -> None:
        """G-007: FillProbResult is frozen (immutable)."""
        model = _make_model(tmp_path)
        features = _make_features()
        result = check_fill_prob(model=model, features=features)
        with pytest.raises(AttributeError):
            result.prob_bps = 9999  # type: ignore[misc]

    def test_unseen_bin_uses_global_prior(self, tmp_path: Path) -> None:
        """G-008: unseen bin → global_prior_bps (5000)."""
        model = _make_model(tmp_path, bins={"short|4|3|4": 9000})
        features = _make_features(direction="long", notional=50.0)
        result = check_fill_prob(model=model, features=features, threshold_bps=2500, enforce=True)
        # long|0|1|0 not in bins → global_prior_bps = 5000
        assert result.verdict == FillProbVerdict.ALLOW
        assert result.prob_bps == 5000


# --- Tests: Engine integration ---------------------------------------------


class TestEngineIntegration:
    """E-001..E-005: fill prob gate wiring in LiveEngineV0."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_gate_blocks_place_when_enforce_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-001: enforce=True + low prob → PLACE blocked with FILL_PROB_LOW."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "5000")

        # price=49000*qty=0.01 → notional=490 → bucket 1 → key long|1|1|0
        model = _make_model(tmp_path, bins={"long|1|1|0": 1000})
        action = _place_action(price="49000.00", qty="0.01")

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        assert la.status == LiveActionStatus.BLOCKED
        assert la.block_reason == BlockReason.FILL_PROB_LOW
        port.place_order.assert_not_called()

    def test_gate_allows_place_when_enforce_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-002: enforce=False → PLACE allowed (shadow mode)."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "0")

        model = _make_model(tmp_path, bins={"long|1|1|0": 1000})
        action = _place_action()

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        assert la.status == LiveActionStatus.EXECUTED
        port.place_order.assert_called_once()

    def test_gate_skips_cancel_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E-003: CANCEL action bypasses fill prob gate entirely."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "10000")  # impossible threshold

        model = _make_model(tmp_path, bins={"long|1|1|0": 0})
        action = _cancel_action()

        port = MagicMock()
        port.cancel_order.return_value = True
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        assert la.status == LiveActionStatus.EXECUTED
        assert la.block_reason is None

    def test_gate_skipped_when_no_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E-004: fill_model=None → gate skipped, action proceeds."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "10000")

        action = _place_action()
        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=None,  # no model
        )

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        assert la.status == LiveActionStatus.EXECUTED

    def test_gate_allows_high_prob(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """E-005: enforce=True + high prob → PLACE allowed."""
        monkeypatch.setenv("GRINDER_FILL_MODEL_ENFORCE", "1")
        monkeypatch.setenv("GRINDER_FILL_PROB_MIN_BPS", "2500")

        model = _make_model(tmp_path, bins={"long|1|1|0": 9000})
        action = _place_action()

        port = MagicMock()
        port.place_order.return_value = "ORDER_1"
        engine = LiveEngineV0(
            paper_engine=_make_paper_engine([action]),
            exchange_port=port,
            config=_live_config(),
            fill_model=model,
        )

        output = engine.process_snapshot(_make_snapshot())
        assert len(output.live_actions) == 1
        la = output.live_actions[0]
        assert la.status == LiveActionStatus.EXECUTED


# --- Tests: Metrics --------------------------------------------------------


class TestMetrics:
    """M-001..M-004: SOR metrics for fill prob gate."""

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()

    def test_block_increments_counter(self) -> None:
        """M-001: fill_prob_blocks_total increments on BLOCK verdict."""
        metrics = get_sor_metrics()
        assert metrics.fill_prob_blocks == 0
        metrics.record_fill_prob_block()
        metrics.record_fill_prob_block()
        assert metrics.fill_prob_blocks == 2

    def test_enforce_enabled_gauge(self) -> None:
        """M-002: fill_prob_enforce_enabled reflects flag state."""
        metrics = get_sor_metrics()
        assert metrics.fill_prob_enforce_enabled is False
        metrics.set_fill_prob_enforce_enabled(True)
        assert metrics.fill_prob_enforce_enabled is True

    def test_prometheus_lines_include_new_metrics(self) -> None:
        """M-003: to_prometheus_lines() includes fill prob metrics."""
        metrics = get_sor_metrics()
        metrics.record_fill_prob_block()
        metrics.set_fill_prob_enforce_enabled(True)
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_router_fill_prob_blocks_total 1" in text
        assert "grinder_router_fill_prob_enforce_enabled 1" in text

    def test_prometheus_lines_defaults(self) -> None:
        """M-004: default metrics → blocks=0, enforce=0."""
        metrics = get_sor_metrics()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_router_fill_prob_blocks_total 0" in text
        assert "grinder_router_fill_prob_enforce_enabled 0" in text


# --- Tests: Contract -------------------------------------------------------


class TestContract:
    """C-001..C-003: metrics contract compliance."""

    def test_required_patterns_present(self) -> None:
        """C-001: REQUIRED_METRICS_PATTERNS includes fill prob gate patterns."""
        expected = [
            "# HELP grinder_router_fill_prob_blocks_total",
            "# TYPE grinder_router_fill_prob_blocks_total",
            "grinder_router_fill_prob_blocks_total",
            "# HELP grinder_router_fill_prob_enforce_enabled",
            "# TYPE grinder_router_fill_prob_enforce_enabled",
            "grinder_router_fill_prob_enforce_enabled",
        ]
        for pattern in expected:
            assert pattern in REQUIRED_METRICS_PATTERNS, f"Missing pattern: {pattern!r}"

    def test_no_forbidden_labels_in_prometheus_output(self) -> None:
        """C-002: Prometheus output contains no FORBIDDEN_METRIC_LABELS."""
        metrics = get_sor_metrics()
        metrics.record_fill_prob_block()
        metrics.set_fill_prob_enforce_enabled(True)
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in text, f"Forbidden label {label!r} found in metrics output"

    def test_metrics_have_help_and_type(self) -> None:
        """C-003: Prometheus output includes HELP and TYPE for fill prob metrics."""
        metrics = get_sor_metrics()
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)
        assert "# HELP grinder_router_fill_prob_blocks_total" in text
        assert "# TYPE grinder_router_fill_prob_blocks_total counter" in text
        assert "# HELP grinder_router_fill_prob_enforce_enabled" in text
        assert "# TYPE grinder_router_fill_prob_enforce_enabled gauge" in text

    def setup_method(self) -> None:
        reset_sor_metrics()

    def teardown_method(self) -> None:
        reset_sor_metrics()
