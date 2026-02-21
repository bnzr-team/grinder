"""Tests for ConsecutiveLossService wiring (PR-C3b).

Covers: config loading, fill conversion, service end-to-end, metrics,
operator override, evidence, error handling, disabled guard, sort stability.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from grinder.observability.metrics_builder import (
    get_consecutive_loss_metrics,
    reset_consecutive_loss_metrics,
    set_consecutive_loss_metrics,
)
from grinder.paper.fills import Fill
from grinder.risk.consecutive_loss_guard import (
    ConsecutiveLossAction,
    ConsecutiveLossConfig,
)
from grinder.risk.consecutive_loss_wiring import (
    ConsecutiveLossService,
    binance_trade_fee,
    binance_trade_to_fill,
    load_consecutive_loss_config,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(
    *,
    trade_id: int,
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: str = "50000.0",
    qty: str = "0.1",
    commission: str = "0.01",
    time: int = 1000000,
    order_id: int = 1,
) -> dict[str, object]:
    """Build a Binance userTrade dict."""
    return {
        "id": trade_id,
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "commission": commission,
        "time": time,
        "orderId": order_id,
    }


def _make_roundtrip_trades(
    symbol: str,
    entry_price: str,
    exit_price: str,
    qty: str,
    base_time: int,
    base_id: int,
) -> list[dict[str, object]]:
    """Build BUY+SELL pair as Binance trade dicts (one roundtrip)."""
    return [
        _make_trade(
            trade_id=base_id,
            symbol=symbol,
            side="BUY",
            price=entry_price,
            qty=qty,
            time=base_time,
            order_id=base_id,
        ),
        _make_trade(
            trade_id=base_id + 1,
            symbol=symbol,
            side="SELL",
            price=exit_price,
            qty=qty,
            time=base_time + 1000,
            order_id=base_id + 1,
        ),
    ]


# ---------------------------------------------------------------------------
# W-001: Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    """W-001: Config loading from environment."""

    def test_default_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default config: disabled, threshold=5, action=PAUSE."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_ENABLED", raising=False)
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_THRESHOLD", raising=False)
        config = load_consecutive_loss_config()
        assert config.enabled is False
        assert config.threshold == 5
        assert config.action == ConsecutiveLossAction.PAUSE

    def test_enabled_custom_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enabled with custom threshold."""
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_ENABLED", "1")
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_THRESHOLD", "3")
        config = load_consecutive_loss_config()
        assert config.enabled is True
        assert config.threshold == 3
        assert config.action == ConsecutiveLossAction.PAUSE


# ---------------------------------------------------------------------------
# W-002: Fill conversion
# ---------------------------------------------------------------------------


class TestFillConversion:
    """W-002: Binance trade → Fill conversion."""

    def test_happy_path(self) -> None:
        """Convert valid Binance trade to Fill."""
        raw = _make_trade(
            trade_id=100,
            symbol="ETHUSDT",
            side="SELL",
            price="3000.50",
            qty="1.5",
            commission="0.05",
            time=1700000000,
            order_id=42,
        )
        fill = binance_trade_to_fill(raw)
        assert fill == Fill(
            ts=1700000000,
            symbol="ETHUSDT",
            side="SELL",
            price=Decimal("3000.50"),
            quantity=Decimal("1.5"),
            order_id="42",
        )
        fee = binance_trade_fee(raw)
        assert fee == Decimal("0.05")

    def test_missing_field(self) -> None:
        """Missing required field raises KeyError."""
        raw = {"id": 1, "symbol": "BTCUSDT"}  # missing many fields
        with pytest.raises(KeyError):
            binance_trade_to_fill(raw)


# ---------------------------------------------------------------------------
# W-003: Service end-to-end
# ---------------------------------------------------------------------------


class TestServiceEndToEnd:
    """W-003: Full service flow."""

    def test_single_buy_no_trip(self) -> None:
        """Single BUY doesn't close a roundtrip → no guard update."""
        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)
        trades = [_make_trade(trade_id=1, side="BUY")]
        svc.process_trades(trades)
        assert svc.guard.count == 0
        assert svc.trip_count == 0

    def test_losing_roundtrip_increments_count(self) -> None:
        """BUY at 100, SELL at 90 → loss → count=1."""
        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)
        assert svc.guard.count == 1
        assert svc.trip_count == 0

    def test_trip_at_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 consecutive losses with threshold=3 → trip."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        svc = ConsecutiveLossService(config)

        for i in range(3):
            base_id = i * 2 + 1
            trades = _make_roundtrip_trades(
                "BTCUSDT", "100.0", "90.0", "1.0", 1000 + i * 2000, base_id
            )
            svc.process_trades(trades)

        assert svc.guard.count == 3
        assert svc.guard.is_tripped is True
        assert svc.trip_count == 1
        assert os.environ.get("GRINDER_OPERATOR_OVERRIDE") == "PAUSE"

    def test_win_resets_count(self) -> None:
        """Loss then win → count resets to 0."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # Loss
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)
        assert svc.guard.count == 1

        # Win
        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "110.0", "1.0", 5000, 3)
        svc.process_trades(trades)
        assert svc.guard.count == 0

    def test_dedup_same_trades_twice(self) -> None:
        """Feeding same trades twice doesn't double-count."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)
        assert svc.guard.count == 1

        # Feed same trades again
        svc.process_trades(trades)
        assert svc.guard.count == 1  # No change


# ---------------------------------------------------------------------------
# W-004: Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    """W-004: Metrics setter/getter."""

    def setup_method(self) -> None:
        reset_consecutive_loss_metrics()

    def test_initial_zero(self) -> None:
        """Initial state is (0, 0)."""
        assert get_consecutive_loss_metrics() == (0, 0)

    def test_after_update(self) -> None:
        """set_consecutive_loss_metrics updates state."""
        set_consecutive_loss_metrics(3, 1)
        assert get_consecutive_loss_metrics() == (3, 1)


# ---------------------------------------------------------------------------
# W-005: Operator override
# ---------------------------------------------------------------------------


class TestOperatorOverride:
    """W-005: PAUSE action sets env var."""

    def test_trip_sets_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guard trip sets GRINDER_OPERATOR_OVERRIDE=PAUSE."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        assert os.environ.get("GRINDER_OPERATOR_OVERRIDE") == "PAUSE"


# ---------------------------------------------------------------------------
# W-006: Evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    """W-006: Evidence artifacts on trip."""

    def test_evidence_written_on_trip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With evidence enabled, trip writes JSON + sha256."""
        monkeypatch.setenv("GRINDER_CONSEC_LOSS_EVIDENCE", "1")
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        risk_dir = tmp_path / "risk"
        json_files = list(risk_dir.glob("consecutive_loss_trip_*.json"))
        sha_files = list(risk_dir.glob("consecutive_loss_trip_*.sha256"))

        assert len(json_files) == 1
        assert len(sha_files) == 1

        # Verify JSON structure
        payload = json.loads(json_files[0].read_text())
        assert payload["artifact_version"] == "consecutive_loss_evidence_v1"
        assert payload["guard_state"]["tripped"] is True
        assert payload["trigger_row"]["outcome"] == "loss"

    def test_evidence_disabled_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With evidence disabled, no files written."""
        monkeypatch.delenv("GRINDER_CONSEC_LOSS_EVIDENCE", raising=False)
        monkeypatch.setenv("GRINDER_ARTIFACT_DIR", str(tmp_path))
        monkeypatch.delenv("GRINDER_OPERATOR_OVERRIDE", raising=False)

        config = ConsecutiveLossConfig(enabled=True, threshold=1)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        risk_dir = tmp_path / "risk"
        assert not risk_dir.exists() or not list(risk_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# W-007: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """W-007: Graceful error handling."""

    def test_bad_trade_skipped(self) -> None:
        """Trade with invalid price is skipped gracefully."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        bad_trade = _make_trade(trade_id=1)
        bad_trade["price"] = "not_a_number"

        svc.process_trades([bad_trade])
        assert svc.guard.count == 0
        assert svc._last_trade_id == 1  # Still advanced past bad trade

    def test_mixed_valid_invalid(self) -> None:
        """Valid trades processed even when mixed with invalid ones."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        bad_trade = _make_trade(trade_id=1)
        bad_trade["price"] = "not_a_number"

        good_trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 2)

        svc.process_trades([bad_trade, *good_trades])
        assert svc.guard.count == 1  # Roundtrip processed
        assert svc._last_trade_id == 3

    def test_missing_id_skipped(self) -> None:
        """Trade without 'id' field is skipped with warning."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        trade_no_id = {"symbol": "BTCUSDT", "side": "BUY", "price": "100.0"}
        svc.process_trades([trade_no_id])
        assert svc.guard.count == 0
        assert svc._last_trade_id == 0


# ---------------------------------------------------------------------------
# W-008: Disabled guard
# ---------------------------------------------------------------------------


class TestDisabledGuard:
    """W-008: Guard disabled = no-op."""

    def test_process_trades_noop(self) -> None:
        """process_trades returns immediately when disabled."""
        config = ConsecutiveLossConfig(enabled=False, threshold=3)
        svc = ConsecutiveLossService(config)

        trades = _make_roundtrip_trades("BTCUSDT", "100.0", "90.0", "1.0", 1000, 1)
        svc.process_trades(trades)

        assert svc.guard.count == 0
        assert svc._last_trade_id == 0


# ---------------------------------------------------------------------------
# W-009: Sort stability
# ---------------------------------------------------------------------------


class TestSortStability:
    """W-009: Out-of-order trades processed correctly."""

    def test_out_of_order_trades(self) -> None:
        """Trades with out-of-order IDs are sorted before processing."""
        config = ConsecutiveLossConfig(enabled=True, threshold=5)
        svc = ConsecutiveLossService(config)

        # Create roundtrip but feed in reverse ID order
        buy = _make_trade(
            trade_id=2,
            side="BUY",
            price="100.0",
            qty="1.0",
            time=1000,
            order_id=2,
        )
        sell = _make_trade(
            trade_id=1,  # Lower ID but should be processed first after sort
            side="SELL",
            price="90.0",
            qty="1.0",
            time=999,
            order_id=1,
        )

        # Feed in wrong order: sell first (id=1), buy second (id=2)
        # After sort by ID: sell(id=1), buy(id=2)
        # sell opens short, buy closes short → roundtrip
        svc.process_trades([buy, sell])

        # Should have processed both (sorted by ID)
        assert svc._last_trade_id == 2
