"""Tests for fill health metrics (Launch-06 PR3).

Covers:
- FillMetrics health counter methods (inc_ingest_polls, inc_ingest_error, etc.)
- FillMetrics.set_ingest_enabled gauge
- Prometheus rendering of new health metrics
- Label allowlist enforcement (no forbidden labels)
- Cursor metrics wiring in load/save
- Ingest metrics wiring in parse/ingest
- Metrics contract satisfaction
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from grinder.execution.fill_cursor import FillCursor, load_fill_cursor, save_fill_cursor
from grinder.execution.fill_ingest import ingest_fills, parse_binance_trade, push_tracker_to_metrics
from grinder.execution.fill_tracker import FillTracker
from grinder.observability.fill_metrics import (
    CURSOR_RESULTS,
    INGEST_ERROR_REASONS,
    FillMetrics,
)
from grinder.observability.metrics_contract import REQUIRED_METRICS_PATTERNS

if TYPE_CHECKING:
    from pathlib import Path


def _make_trade(
    *,
    trade_id: int = 100,
    time_ms: int = 1700000000000,
    side: str = "BUY",
    maker: bool = False,
    qty: str = "0.001",
    price: str = "50000.0",
    commission: str = "0.025",
) -> dict[str, Any]:
    return {
        "id": trade_id,
        "time": time_ms,
        "symbol": "BTCUSDT",
        "side": side,
        "maker": maker,
        "qty": qty,
        "price": price,
        "commission": commission,
        "commissionAsset": "USDT",
        "orderId": 99999,
        "buyer": side == "BUY",
        "realizedPnl": "0",
        "positionSide": "BOTH",
        "quoteQty": str(float(qty) * float(price)),
    }


# =============================================================================
# FillMetrics health counter methods
# =============================================================================


class TestFillMetricsHealthCounters:
    def test_inc_ingest_polls(self) -> None:
        m = FillMetrics()
        m.inc_ingest_polls("reconcile")
        m.inc_ingest_polls("reconcile")
        assert m.ingest_polls["reconcile"] == 2

    def test_set_ingest_enabled_on(self) -> None:
        m = FillMetrics()
        m.set_ingest_enabled("reconcile", True)
        assert m.ingest_enabled["reconcile"] == 1

    def test_set_ingest_enabled_off(self) -> None:
        m = FillMetrics()
        m.set_ingest_enabled("reconcile", False)
        assert m.ingest_enabled["reconcile"] == 0

    def test_set_ingest_enabled_toggle(self) -> None:
        m = FillMetrics()
        m.set_ingest_enabled("reconcile", True)
        assert m.ingest_enabled["reconcile"] == 1
        m.set_ingest_enabled("reconcile", False)
        assert m.ingest_enabled["reconcile"] == 0

    def test_inc_ingest_error_valid_reasons(self) -> None:
        m = FillMetrics()
        for reason in INGEST_ERROR_REASONS:
            m.inc_ingest_error("reconcile", reason)
        assert len(m.ingest_errors) == len(INGEST_ERROR_REASONS)

    def test_inc_ingest_error_unknown_reason_maps_to_unknown(self) -> None:
        m = FillMetrics()
        m.inc_ingest_error("reconcile", "weird_reason")
        assert m.ingest_errors[("reconcile", "unknown")] == 1

    def test_inc_cursor_load_ok(self) -> None:
        m = FillMetrics()
        m.inc_cursor_load("reconcile", "ok")
        assert m.cursor_loads[("reconcile", "ok")] == 1

    def test_inc_cursor_load_error(self) -> None:
        m = FillMetrics()
        m.inc_cursor_load("reconcile", "error")
        assert m.cursor_loads[("reconcile", "error")] == 1

    def test_inc_cursor_load_invalid_result_maps_to_error(self) -> None:
        m = FillMetrics()
        m.inc_cursor_load("reconcile", "bad")
        assert m.cursor_loads[("reconcile", "error")] == 1

    def test_inc_cursor_save_ok(self) -> None:
        m = FillMetrics()
        m.inc_cursor_save("reconcile", "ok")
        assert m.cursor_saves[("reconcile", "ok")] == 1

    def test_inc_cursor_save_invalid_result_maps_to_error(self) -> None:
        m = FillMetrics()
        m.inc_cursor_save("reconcile", "nope")
        assert m.cursor_saves[("reconcile", "error")] == 1

    def test_reset_clears_health_counters(self) -> None:
        m = FillMetrics()
        m.inc_ingest_polls("reconcile")
        m.set_ingest_enabled("reconcile", True)
        m.inc_ingest_error("reconcile", "parse")
        m.inc_cursor_load("reconcile", "ok")
        m.inc_cursor_save("reconcile", "ok")
        m.reset()
        assert len(m.ingest_polls) == 0
        assert len(m.ingest_enabled) == 0
        assert len(m.ingest_errors) == 0
        assert len(m.cursor_loads) == 0
        assert len(m.cursor_saves) == 0


# =============================================================================
# Prometheus rendering of health metrics
# =============================================================================


class TestFillMetricsPrometheus:
    def test_health_metrics_in_output_default(self) -> None:
        """Default (empty) state renders placeholder lines."""
        m = FillMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_fill_ingest_polls_total" in text
        assert "grinder_fill_ingest_enabled" in text
        assert "grinder_fill_ingest_errors_total" in text
        assert "grinder_fill_cursor_load_total" in text
        assert "grinder_fill_cursor_save_total" in text

    def test_ingest_polls_render(self) -> None:
        m = FillMetrics()
        m.inc_ingest_polls("reconcile")
        lines = m.to_prometheus_lines()
        assert any(
            'grinder_fill_ingest_polls_total{source="reconcile"} 1' in line for line in lines
        )

    def test_enabled_gauge_render(self) -> None:
        m = FillMetrics()
        m.set_ingest_enabled("reconcile", True)
        lines = m.to_prometheus_lines()
        assert any('grinder_fill_ingest_enabled{source="reconcile"} 1' in line for line in lines)

    def test_error_counter_render(self) -> None:
        m = FillMetrics()
        m.inc_ingest_error("reconcile", "parse")
        lines = m.to_prometheus_lines()
        assert any(
            'grinder_fill_ingest_errors_total{source="reconcile",reason="parse"} 1' in line
            for line in lines
        )

    def test_cursor_load_render(self) -> None:
        m = FillMetrics()
        m.inc_cursor_load("reconcile", "ok")
        lines = m.to_prometheus_lines()
        assert any(
            'grinder_fill_cursor_load_total{source="reconcile",result="ok"} 1' in line
            for line in lines
        )

    def test_cursor_save_render(self) -> None:
        m = FillMetrics()
        m.inc_cursor_save("reconcile", "error")
        lines = m.to_prometheus_lines()
        assert any(
            'grinder_fill_cursor_save_total{source="reconcile",result="error"} 1' in line
            for line in lines
        )

    def test_no_forbidden_labels_in_health_metrics(self) -> None:
        m = FillMetrics()
        m.inc_ingest_polls("reconcile")
        m.set_ingest_enabled("reconcile", True)
        m.inc_ingest_error("reconcile", "http")
        m.inc_cursor_load("reconcile", "ok")
        m.inc_cursor_save("reconcile", "ok")
        text = "\n".join(m.to_prometheus_lines())
        assert "symbol=" not in text
        assert "order_id=" not in text
        assert "client_id=" not in text
        assert "trade_id=" not in text

    def test_reason_allowlist_exhaustive(self) -> None:
        expected = {"http", "parse", "cursor", "unknown"}
        assert expected == INGEST_ERROR_REASONS

    def test_result_allowlist_exhaustive(self) -> None:
        expected = {"ok", "error"}
        assert expected == CURSOR_RESULTS


# =============================================================================
# Cursor metrics wiring
# =============================================================================


class TestCursorMetricsWiring:
    def test_load_ok_increments_metric(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text(json.dumps({"last_trade_id": 42}))
        m = FillMetrics()
        load_fill_cursor(str(path), fill_metrics=m)
        assert m.cursor_loads[("reconcile", "ok")] == 1

    def test_load_corrupt_increments_error(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text("NOT JSON")
        m = FillMetrics()
        load_fill_cursor(str(path), fill_metrics=m)
        assert m.cursor_loads[("reconcile", "error")] == 1

    def test_load_missing_no_metric(self, tmp_path: Path) -> None:
        """Missing file is not an error — no metric increment."""
        m = FillMetrics()
        load_fill_cursor(str(tmp_path / "nope.json"), fill_metrics=m)
        assert len(m.cursor_loads) == 0

    def test_save_ok_increments_metric(self, tmp_path: Path) -> None:
        m = FillMetrics()
        cursor = FillCursor(last_trade_id=1)
        save_fill_cursor(str(tmp_path / "c.json"), cursor, 100, fill_metrics=m)
        assert m.cursor_saves[("reconcile", "ok")] == 1

    def test_save_error_increments_metric(self) -> None:
        m = FillMetrics()
        cursor = FillCursor(last_trade_id=1)
        # /dev/null/impossible is not writable
        save_fill_cursor("/dev/null/impossible/c.json", cursor, 100, fill_metrics=m)
        assert m.cursor_saves[("reconcile", "error")] == 1

    def test_load_without_metrics_still_works(self, tmp_path: Path) -> None:
        """Backward compatibility: no metrics param."""
        path = tmp_path / "cursor.json"
        path.write_text(json.dumps({"last_trade_id": 5}))
        cursor = load_fill_cursor(str(path))
        assert cursor.last_trade_id == 5

    def test_save_without_metrics_still_works(self, tmp_path: Path) -> None:
        cursor = FillCursor(last_trade_id=7)
        save_fill_cursor(str(tmp_path / "c.json"), cursor, 100)
        data = json.loads((tmp_path / "c.json").read_text())
        assert data["last_trade_id"] == 7


# =============================================================================
# Ingest metrics wiring
# =============================================================================


class TestIngestMetricsWiring:
    def test_ingest_increments_polls(self) -> None:
        m = FillMetrics()
        tracker = FillTracker()
        cursor = FillCursor()
        ingest_fills([], tracker, cursor, fill_metrics=m)
        assert m.ingest_polls["reconcile"] == 1

    def test_ingest_increments_polls_every_call(self) -> None:
        m = FillMetrics()
        tracker = FillTracker()
        cursor = FillCursor()
        for _ in range(3):
            ingest_fills([], tracker, cursor, fill_metrics=m)
        assert m.ingest_polls["reconcile"] == 3

    def test_parse_error_increments_error_metric(self) -> None:
        m = FillMetrics()
        bad_trade: dict[str, Any] = {"id": 1}  # missing required fields
        result = parse_binance_trade(bad_trade, fill_metrics=m)
        assert result is None
        assert m.ingest_errors[("reconcile", "parse")] == 1

    def test_parse_success_no_error_metric(self) -> None:
        m = FillMetrics()
        result = parse_binance_trade(_make_trade(), fill_metrics=m)
        assert result is not None
        assert len(m.ingest_errors) == 0

    def test_ingest_with_bad_trade_increments_parse_error(self) -> None:
        m = FillMetrics()
        tracker = FillTracker()
        cursor = FillCursor()
        trades: list[dict[str, Any]] = [_make_trade(trade_id=1), {"id": 2}]
        count = ingest_fills(trades, tracker, cursor, fill_metrics=m)
        assert count == 1
        assert m.ingest_errors[("reconcile", "parse")] == 1
        assert m.ingest_polls["reconcile"] == 1

    def test_ingest_without_metrics_backward_compat(self) -> None:
        """No fill_metrics param — still works."""
        tracker = FillTracker()
        cursor = FillCursor()
        count = ingest_fills([_make_trade(trade_id=1)], tracker, cursor)
        assert count == 1


# =============================================================================
# Metrics contract
# =============================================================================


class TestFillHealthMetricsContract:
    def test_contract_patterns_satisfied(self) -> None:
        """All PR3 fill health patterns in metrics_contract are satisfied."""
        m = FillMetrics()
        m.inc_ingest_polls("reconcile")
        m.set_ingest_enabled("reconcile", True)
        m.inc_ingest_error("reconcile", "parse")
        m.inc_cursor_load("reconcile", "ok")
        m.inc_cursor_save("reconcile", "ok")

        # Also add a fill event so all fill patterns work
        m.record_fill("reconcile", "buy", "taker", 100.0, 0.05)

        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        fill_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "fill" in p.lower()]
        for pattern in fill_patterns:
            assert pattern in text, f"Missing contract pattern: {pattern}"

    def test_full_pipeline_with_health(self) -> None:
        """Full pipeline: ingest with metrics -> push -> contract check."""
        tracker = FillTracker()
        cursor = FillCursor()
        m = FillMetrics()
        m.set_ingest_enabled("reconcile", True)
        m.inc_cursor_load("reconcile", "ok")

        trades = [_make_trade(trade_id=1)]
        ingest_fills(trades, tracker, cursor, fill_metrics=m)
        push_tracker_to_metrics(tracker, m)
        m.inc_cursor_save("reconcile", "ok")

        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        fill_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "fill" in p.lower()]
        for pattern in fill_patterns:
            assert pattern in text, f"Missing contract pattern: {pattern}"
