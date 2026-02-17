"""Tests for data quality wiring into LiveFeed (Launch-03 PR2).

Tests verify:
- DataQualityEngine: observe_tick increments all 3 metric families
- LiveFeed wiring: dq_enabled=True → DQ metrics increment on real snapshots
- LiveFeed wiring: dq_enabled=False → DQ metrics remain zero
- No forbidden labels in output
- Deterministic: no time.time() in DQ path (explicit ts_ms)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.contracts import Snapshot
from grinder.data.quality import DataQualityConfig
from grinder.data.quality_engine import DataQualityEngine
from grinder.data.quality_metrics import (
    METRIC_DATA_GAP,
    METRIC_DATA_OUTLIER,
    METRIC_DATA_STALE,
    get_data_quality_metrics,
    reset_data_quality_metrics,
)
from grinder.live.feed import LiveFeed, LiveFeedConfig
from grinder.observability.metrics_contract import FORBIDDEN_METRIC_LABELS


@pytest.fixture(autouse=True)
def _reset_dq_metrics() -> None:
    """Reset data quality metrics before each test."""
    reset_data_quality_metrics()


def _make_snapshot(
    ts: int,
    bid: str = "50000",
    ask: str = "50001",
    symbol: str = "BTCUSDT",
) -> Snapshot:
    """Helper to build a Snapshot with sensible defaults."""
    return Snapshot(
        ts=ts,
        symbol=symbol,
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        last_price=Decimal(bid),
        last_qty=Decimal("0.5"),
    )


# ---------------------------------------------------------------------------
# DataQualityEngine unit tests
# ---------------------------------------------------------------------------


class TestDataQualityEngine:
    """Tests for the DataQualityEngine wrapper."""

    def test_gap_detection_increments_metric(self) -> None:
        """Gap > bucket threshold increments gap counter."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000, 5000))
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        dq.observe_tick(stream="test", ts_ms=2000, price=100.0)  # 1000ms gap → bucket "500"

        assert metrics.gap_counts.get(("test", "500"), 0) == 1

    def test_no_gap_below_threshold(self) -> None:
        """Gap below smallest bucket does NOT increment."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000))
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        dq.observe_tick(stream="test", ts_ms=1400, price=100.0)  # 400ms < 500

        assert len(metrics.gap_counts) == 0

    def test_outlier_detection_increments_metric(self) -> None:
        """Price jump > threshold increments outlier counter."""
        cfg = DataQualityConfig(price_jump_max_bps=100)
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        dq.observe_tick(stream="test", ts_ms=2000, price=120.0)  # 2000 bps > 100

        assert metrics.outlier_counts.get(("test", "price"), 0) == 1

    def test_no_outlier_within_threshold(self) -> None:
        """Small price change does NOT increment outlier counter."""
        cfg = DataQualityConfig(price_jump_max_bps=500)
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        dq.observe_tick(stream="test", ts_ms=2000, price=101.0)  # 100 bps < 500

        assert len(metrics.outlier_counts) == 0

    def test_staleness_detection_increments_metric(self) -> None:
        """Stale tick (now - last_ts > threshold) increments stale counter."""
        cfg = DataQualityConfig(stale_book_ticker_ms=1000)
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        # First tick sets last_ts
        dq.observe_tick(stream="live_feed", ts_ms=1000, price=100.0, now_ms=1000)
        # Second tick: now is 3000, last_ts was 1000, diff=2000 > 1000 → stale
        dq.observe_tick(stream="live_feed", ts_ms=3000, price=100.0, now_ms=3000)

        assert metrics.stale_counts.get("live_feed", 0) == 1

    def test_no_staleness_without_now_ms(self) -> None:
        """Staleness check is skipped when now_ms is None."""
        cfg = DataQualityConfig(stale_book_ticker_ms=1)
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        dq.observe_tick(stream="test", ts_ms=9999, price=100.0)

        assert len(metrics.stale_counts) == 0

    def test_first_tick_no_events(self) -> None:
        """First tick per stream produces no gap/outlier/stale events."""
        dq = DataQualityEngine()
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="test", ts_ms=1000, price=50000.0, now_ms=1000)

        assert len(metrics.gap_counts) == 0
        assert len(metrics.outlier_counts) == 0
        assert len(metrics.stale_counts) == 0

    def test_reset_clears_state(self) -> None:
        """Reset clears internal state so next tick is treated as first."""
        dq = DataQualityEngine()
        dq.observe_tick(stream="test", ts_ms=1000, price=100.0)
        dq.reset()
        # After reset, this should be treated as first tick (no gap/outlier)
        metrics = get_data_quality_metrics()
        metrics.reset()
        dq.observe_tick(stream="test", ts_ms=9999, price=999.0)
        assert len(metrics.gap_counts) == 0
        assert len(metrics.outlier_counts) == 0

    def test_multiple_streams_independent(self) -> None:
        """Different streams maintain independent DQ state."""
        cfg = DataQualityConfig(gap_buckets_ms=(500,), price_jump_max_bps=100)
        dq = DataQualityEngine(cfg)
        metrics = get_data_quality_metrics()

        dq.observe_tick(stream="a", ts_ms=1000, price=100.0)
        dq.observe_tick(stream="b", ts_ms=5000, price=200.0)

        # Stream "a" next tick at ts=1100 (100ms gap, < 500) — no gap
        dq.observe_tick(stream="a", ts_ms=1100, price=100.5)
        # Stream "b" next tick at ts=5100 (100ms gap, < 500) — no gap
        dq.observe_tick(stream="b", ts_ms=5100, price=200.5)

        assert len(metrics.gap_counts) == 0


# ---------------------------------------------------------------------------
# LiveFeed wiring tests (integration-style)
# ---------------------------------------------------------------------------


class TestLiveFeedDqWiring:
    """Integration-style tests: DQ wired into LiveFeed._process_snapshot."""

    def test_dq_enabled_increments_gap_counter(self) -> None:
        """With dq_enabled=True, gap events increment metrics via LiveFeed."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="live_feed",
        )
        feed = LiveFeed(config, clock=lambda: 1.0)
        metrics = get_data_quality_metrics()

        # Tick 1
        snap1 = _make_snapshot(ts=1_000_000)
        feed.process_snapshot_sync(snap1)

        # Tick 2: 1000ms later (gap >= 500ms default bucket)
        snap2 = _make_snapshot(ts=1_001_000)
        feed.process_snapshot_sync(snap2)

        assert sum(metrics.gap_counts.values()) >= 1

    def test_dq_enabled_increments_outlier_counter(self) -> None:
        """With dq_enabled=True, outlier events increment metrics via LiveFeed."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="live_feed",
        )
        feed = LiveFeed(config, clock=lambda: 1.0)
        metrics = get_data_quality_metrics()

        # Tick 1: normal price
        snap1 = _make_snapshot(ts=1_000_000, bid="50000", ask="50001")
        feed.process_snapshot_sync(snap1)

        # Tick 2: massive price jump (50000 → 60000 = 2000 bps > 500 default)
        snap2 = _make_snapshot(ts=1_000_100, bid="60000", ask="60001")
        feed.process_snapshot_sync(snap2)

        assert metrics.outlier_counts.get(("live_feed", "price"), 0) >= 1

    def test_dq_enabled_increments_stale_counter(self) -> None:
        """With dq_enabled=True, staleness events increment metrics via LiveFeed."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="live_feed",
        )
        # Clock returns 1.0 → now_ms = 1000
        # First tick ts=100 → last_ts set to 100
        # Next call clock still 1.0 → now_ms=1000, last_ts=100, diff=900 < 2000 → no stale
        # We need clock to return a time that makes now_ms - last_ts > threshold
        call_count = 0

        def mock_clock() -> float:
            nonlocal call_count
            call_count += 1
            # _process_snapshot calls clock twice: start_ts and end_ts
            # Tick 1: calls 1,2 → start_ts=1000
            # Tick 2: calls 3,4 → start_ts=5000 (5s later)
            if call_count <= 2:
                return 1.0  # 1000ms
            return 5.0  # 5000ms

        feed = LiveFeed(config, clock=mock_clock)
        metrics = get_data_quality_metrics()

        # Tick 1: ts=1000
        snap1 = _make_snapshot(ts=1000)
        feed.process_snapshot_sync(snap1)

        # Tick 2: ts=2000 but now_ms=5000 → diff = 5000-1000 = 4000 > 2000 (default stale threshold)
        snap2 = _make_snapshot(ts=2000)
        feed.process_snapshot_sync(snap2)

        assert metrics.stale_counts.get("live_feed", 0) >= 1

    def test_dq_disabled_no_metric_increments(self) -> None:
        """With dq_enabled=False (default), DQ metrics remain zero."""
        config = LiveFeedConfig(symbols=["BTCUSDT"], dq_enabled=False)
        feed = LiveFeed(config, clock=lambda: 1.0)
        metrics = get_data_quality_metrics()

        # Send multiple snapshots with gaps and price jumps
        snap1 = _make_snapshot(ts=1_000_000, bid="50000", ask="50001")
        feed.process_snapshot_sync(snap1)

        snap2 = _make_snapshot(ts=1_010_000, bid="60000", ask="60001")
        feed.process_snapshot_sync(snap2)

        assert len(metrics.gap_counts) == 0
        assert len(metrics.outlier_counts) == 0
        assert len(metrics.stale_counts) == 0

    def test_dq_disabled_same_features_output(self) -> None:
        """DQ wiring does not alter LiveFeaturesUpdate output."""
        snap = _make_snapshot(ts=1_000_000)

        # Without DQ
        config_off = LiveFeedConfig(symbols=["BTCUSDT"], dq_enabled=False)
        feed_off = LiveFeed(config_off, clock=lambda: 1.0)
        update_off = feed_off.process_snapshot_sync(snap)

        # With DQ
        reset_data_quality_metrics()
        config_on = LiveFeedConfig(symbols=["BTCUSDT"], dq_enabled=True)
        feed_on = LiveFeed(config_on, clock=lambda: 1.0)
        update_on = feed_on.process_snapshot_sync(snap)

        assert update_off is not None
        assert update_on is not None
        assert update_off.ts == update_on.ts
        assert update_off.symbol == update_on.symbol
        assert update_off.bars_available == update_on.bars_available

    def test_dq_stream_label_is_stable(self) -> None:
        """Stream label comes from config, not from symbol."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="my_custom_stream",
        )
        feed = LiveFeed(config, clock=lambda: 1.0)
        metrics = get_data_quality_metrics()

        snap1 = _make_snapshot(ts=1_000_000)
        feed.process_snapshot_sync(snap1)
        snap2 = _make_snapshot(ts=1_001_000)
        feed.process_snapshot_sync(snap2)

        # Gap counter should use "my_custom_stream", not "BTCUSDT"
        gap_streams = {k[0] for k in metrics.gap_counts}
        assert "my_custom_stream" in gap_streams or len(metrics.gap_counts) == 0
        # And definitely not the symbol
        assert "BTCUSDT" not in gap_streams

    def test_no_forbidden_labels_in_wired_metrics(self) -> None:
        """DQ metrics from wired path contain no forbidden labels."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="live_feed",
        )
        feed = LiveFeed(config, clock=lambda: 1.0)

        snap1 = _make_snapshot(ts=1_000_000, bid="50000", ask="50001")
        feed.process_snapshot_sync(snap1)
        snap2 = _make_snapshot(ts=1_001_000, bid="55000", ask="55001")
        feed.process_snapshot_sync(snap2)

        metrics = get_data_quality_metrics()
        text = "\n".join(metrics.to_prometheus_lines())

        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in text, f"Forbidden label {label!r} in wired DQ metrics"

    def test_multiple_snapshots_accumulate(self) -> None:
        """Multiple snapshots with gaps accumulate gap counters."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="live_feed",
        )
        feed = LiveFeed(config, clock=lambda: 1.0)
        metrics = get_data_quality_metrics()

        # 5 snapshots, each 1000ms apart (gap >= 500ms bucket)
        for i in range(5):
            snap = _make_snapshot(ts=1_000_000 + i * 1000)
            feed.process_snapshot_sync(snap)

        # 4 gaps (first tick has no gap)
        total_gaps = sum(metrics.gap_counts.values())
        assert total_gaps == 4


# ---------------------------------------------------------------------------
# Prometheus output tests (end-to-end render)
# ---------------------------------------------------------------------------


class TestDqMetricsRender:
    """Verify DQ metrics render correctly after wiring."""

    def test_prometheus_output_has_all_families(self) -> None:
        """After wiring, all 3 DQ metric families appear in Prometheus output."""
        config = LiveFeedConfig(
            symbols=["BTCUSDT"],
            dq_enabled=True,
            dq_stream="live_feed",
        )
        feed = LiveFeed(config, clock=lambda: 1.0)

        # Tick 1
        feed.process_snapshot_sync(_make_snapshot(ts=1_000_000, bid="50000", ask="50001"))
        # Tick 2: big gap + price jump
        feed.process_snapshot_sync(_make_snapshot(ts=1_010_000, bid="60000", ask="60001"))

        metrics = get_data_quality_metrics()
        text = "\n".join(metrics.to_prometheus_lines())

        assert METRIC_DATA_STALE in text or f"# HELP {METRIC_DATA_STALE}" in text
        assert METRIC_DATA_GAP in text
        assert METRIC_DATA_OUTLIER in text
