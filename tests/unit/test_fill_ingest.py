"""Tests for fill ingestion: Binance trade parsing + wiring (Launch-06 PR2)."""

from __future__ import annotations

import time
from typing import Any

from grinder.execution.fill_cursor import FillCursor
from grinder.execution.fill_ingest import ingest_fills, parse_binance_trade, push_tracker_to_metrics
from grinder.execution.fill_tracker import FillTracker
from grinder.observability.fill_metrics import FillMetrics
from grinder.observability.metrics_contract import REQUIRED_METRICS_PATTERNS


def _make_trade(
    *,
    trade_id: int = 100,
    time_ms: int = 1700000000000,
    side: str = "BUY",
    maker: bool = False,
    qty: str = "0.001",
    price: str = "50000.0",
    commission: str = "0.025",
    commission_asset: str = "USDT",
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    """Build a Binance-like userTrade dict."""
    return {
        "id": trade_id,
        "time": time_ms,
        "symbol": symbol,
        "side": side,
        "maker": maker,
        "qty": qty,
        "price": price,
        "commission": commission,
        "commissionAsset": commission_asset,
        "orderId": 99999,
        "buyer": side == "BUY",
        "realizedPnl": "0",
        "positionSide": "BOTH",
        "quoteQty": str(float(qty) * float(price)),
    }


# =============================================================================
# parse_binance_trade
# =============================================================================


class TestParseBinanceTrade:
    """Tests for parse_binance_trade()."""

    def test_buy_taker(self) -> None:
        raw = _make_trade(side="BUY", maker=False)
        event = parse_binance_trade(raw)
        assert event is not None
        assert event.source == "reconcile"
        assert event.side == "buy"
        assert event.liquidity == "taker"
        assert event.qty == 0.001
        assert event.price == 50000.0
        assert event.fee == 0.025
        assert event.fee_asset == "USDT"

    def test_sell_maker(self) -> None:
        raw = _make_trade(side="SELL", maker=True)
        event = parse_binance_trade(raw)
        assert event is not None
        assert event.side == "sell"
        assert event.liquidity == "maker"

    def test_timestamp_preserved(self) -> None:
        raw = _make_trade(time_ms=1700123456789)
        event = parse_binance_trade(raw)
        assert event is not None
        assert event.ts_ms == 1700123456789

    def test_missing_time_returns_none(self) -> None:
        raw = _make_trade()
        del raw["time"]
        event = parse_binance_trade(raw)
        assert event is None

    def test_missing_qty_returns_none(self) -> None:
        raw = _make_trade()
        del raw["qty"]
        event = parse_binance_trade(raw)
        assert event is None

    def test_invalid_side_maps_to_none(self) -> None:
        raw = _make_trade(side="UNKNOWN")
        event = parse_binance_trade(raw)
        assert event is not None
        assert event.side == "none"

    def test_source_always_reconcile(self) -> None:
        raw = _make_trade()
        event = parse_binance_trade(raw)
        assert event is not None
        assert event.source == "reconcile"

    def test_no_forbidden_labels(self) -> None:
        """Parsed event must NOT contain symbol/order_id as metric labels."""
        raw = _make_trade(symbol="BTCUSDT")
        event = parse_binance_trade(raw)
        assert event is not None
        # source/side/liquidity are the only label-relevant fields
        assert event.source in {"reconcile", "sim", "manual", "none"}
        assert event.side in {"buy", "sell", "none"}
        assert event.liquidity in {"maker", "taker", "none"}


# =============================================================================
# ingest_fills
# =============================================================================


class TestIngestFills:
    """Tests for ingest_fills()."""

    def test_records_new_fills(self) -> None:
        trades = [_make_trade(trade_id=10), _make_trade(trade_id=11)]
        tracker = FillTracker()
        cursor = FillCursor()
        count = ingest_fills(trades, tracker, cursor)
        assert count == 2
        snap = tracker.snapshot()
        assert snap.total_fills == 2

    def test_deduplicates_by_cursor(self) -> None:
        """Trades with id <= cursor.last_trade_id are skipped."""
        trades = [_make_trade(trade_id=10), _make_trade(trade_id=11)]
        tracker = FillTracker()
        cursor = FillCursor(last_trade_id=10)
        count = ingest_fills(trades, tracker, cursor)
        assert count == 1
        assert cursor.last_trade_id == 11

    def test_cursor_advances_to_max_id(self) -> None:
        trades = [
            _make_trade(trade_id=5, time_ms=1000),
            _make_trade(trade_id=8, time_ms=2000),
            _make_trade(trade_id=6, time_ms=1500),
        ]
        tracker = FillTracker()
        cursor = FillCursor()
        ingest_fills(trades, tracker, cursor)
        assert cursor.last_trade_id == 8
        assert cursor.last_ts_ms == 2000

    def test_empty_trades(self) -> None:
        tracker = FillTracker()
        cursor = FillCursor(last_trade_id=5)
        count = ingest_fills([], tracker, cursor)
        assert count == 0
        assert cursor.last_trade_id == 5

    def test_all_trades_already_seen(self) -> None:
        trades = [_make_trade(trade_id=3), _make_trade(trade_id=5)]
        tracker = FillTracker()
        cursor = FillCursor(last_trade_id=10)
        count = ingest_fills(trades, tracker, cursor)
        assert count == 0

    def test_invalid_trade_skipped(self) -> None:
        """A trade missing required fields is skipped (not crash)."""
        good = _make_trade(trade_id=10)
        bad = {"id": 11}  # missing time, qty, price, commission
        trades = [good, bad]
        tracker = FillTracker()
        cursor = FillCursor()
        count = ingest_fills(trades, tracker, cursor)
        assert count == 1
        assert tracker.snapshot().total_fills == 1

    def test_buy_sell_split(self) -> None:
        trades = [
            _make_trade(trade_id=1, side="BUY", qty="1.0", price="100.0"),
            _make_trade(trade_id=2, side="SELL", qty="2.0", price="200.0"),
        ]
        tracker = FillTracker()
        cursor = FillCursor()
        ingest_fills(trades, tracker, cursor)
        snap = tracker.snapshot()
        assert snap.buy_fills == 1
        assert snap.sell_fills == 1
        assert snap.buy_notional == 100.0
        assert snap.sell_notional == 400.0

    def test_maker_taker_split(self) -> None:
        trades = [
            _make_trade(trade_id=1, maker=True),
            _make_trade(trade_id=2, maker=False),
        ]
        tracker = FillTracker()
        cursor = FillCursor()
        ingest_fills(trades, tracker, cursor)
        snap = tracker.snapshot()
        assert snap.maker_fills == 1
        assert snap.taker_fills == 1


# =============================================================================
# push_tracker_to_metrics
# =============================================================================


class TestPushTrackerToMetrics:
    """Tests for push_tracker_to_metrics()."""

    def test_mirrors_tracker_state(self) -> None:
        tracker = FillTracker()
        event = parse_binance_trade(_make_trade(trade_id=1, side="BUY", maker=True))
        assert event is not None
        tracker.record(event)
        metrics = FillMetrics()
        push_tracker_to_metrics(tracker, metrics)
        assert len(metrics.fills) > 0

    def test_metrics_produce_prometheus_lines(self) -> None:
        """After push, FillMetrics renders non-placeholder lines."""
        tracker = FillTracker()
        event = parse_binance_trade(_make_trade(trade_id=1))
        assert event is not None
        tracker.record(event)
        metrics = FillMetrics()
        push_tracker_to_metrics(tracker, metrics)
        lines = metrics.to_prometheus_lines()
        # Should have real values, not just placeholders
        assert any('source="reconcile"' in line for line in lines)

    def test_no_forbidden_labels_in_prometheus(self) -> None:
        """Prometheus output must not contain symbol, order_id, etc."""
        tracker = FillTracker()
        event = parse_binance_trade(_make_trade(trade_id=1))
        assert event is not None
        tracker.record(event)
        metrics = FillMetrics()
        push_tracker_to_metrics(tracker, metrics)
        lines = metrics.to_prometheus_lines()
        joined = "\n".join(lines)
        assert "symbol=" not in joined
        assert "order_id=" not in joined
        assert "client_id=" not in joined

    def test_allowlisted_labels_only(self) -> None:
        """Only source, side, liquidity appear as labels."""
        tracker = FillTracker()
        event = parse_binance_trade(_make_trade(trade_id=1))
        assert event is not None
        tracker.record(event)
        metrics = FillMetrics()
        push_tracker_to_metrics(tracker, metrics)
        for key in metrics.fills:
            assert len(key) == 3  # (source, side, liquidity)

    def test_cumulative_across_pushes(self) -> None:
        """FillTracker is cumulative, so repeated push gives latest totals."""
        tracker = FillTracker()
        metrics = FillMetrics()

        event1 = parse_binance_trade(_make_trade(trade_id=1, qty="1.0", price="100.0"))
        assert event1 is not None
        tracker.record(event1)
        push_tracker_to_metrics(tracker, metrics)
        assert metrics.fills[("reconcile", "buy", "taker")] == 1

        event2 = parse_binance_trade(_make_trade(trade_id=2, qty="2.0", price="200.0"))
        assert event2 is not None
        tracker.record(event2)
        push_tracker_to_metrics(tracker, metrics)
        assert metrics.fills[("reconcile", "buy", "taker")] == 2


# =============================================================================
# Integration-ish: FakePort -> ingest -> metrics
# =============================================================================


class TestFillIntegration:
    """Integration test: raw trades -> FillTracker -> FillMetrics -> Prometheus."""

    def test_full_pipeline(self) -> None:
        """Simulates what fetch_snapshot does: fetch trades, ingest, push to metrics."""
        # Fake port returns
        raw_trades = [
            _make_trade(
                trade_id=1, side="BUY", maker=False, qty="0.5", price="1000.0", commission="0.5"
            ),
            _make_trade(
                trade_id=2, side="SELL", maker=True, qty="1.0", price="2000.0", commission="1.0"
            ),
            _make_trade(
                trade_id=3, side="BUY", maker=True, qty="0.1", price="500.0", commission="0.025"
            ),
        ]

        tracker = FillTracker()
        cursor = FillCursor()
        metrics = FillMetrics()

        # Ingest
        count = ingest_fills(raw_trades, tracker, cursor)
        assert count == 3
        assert cursor.last_trade_id == 3

        # Push to metrics
        push_tracker_to_metrics(tracker, metrics)

        # Verify Prometheus output
        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)

        # Should have real counters
        assert "grinder_fills_total" in text
        assert "grinder_fill_notional_total" in text
        assert "grinder_fill_fees_total" in text
        assert 'source="reconcile"' in text

        # Should not have forbidden labels
        assert "symbol=" not in text
        assert "order_id=" not in text

        # Second iteration: all trades already seen
        count2 = ingest_fills(raw_trades, tracker, cursor)
        assert count2 == 0  # cursor dedup

    def test_contract_satisfied_after_ingestion(self) -> None:
        """After real fills, metrics contract patterns should still be satisfied."""
        tracker = FillTracker()
        cursor = FillCursor()
        metrics = FillMetrics()

        raw_trades = [_make_trade(trade_id=1)]
        ingest_fills(raw_trades, tracker, cursor)
        push_tracker_to_metrics(tracker, metrics)
        # PR6: cursor stuck detection metrics (needed for contract)
        metrics.set_cursor_last_save_ts("reconcile", time.time())

        lines = metrics.to_prometheus_lines()
        text = "\n".join(lines)

        # Check fill-related contract patterns
        fill_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "fill" in p.lower()]
        for pattern in fill_patterns:
            assert pattern in text, f"Missing contract pattern: {pattern}"
