"""Tests for data quality detectors and metrics (Launch-03).

Tests verify:
- GapDetector: first-tick no-gap, bucket classification, per-stream isolation
- OutlierFilter: first-price no-outlier, threshold logic, always-update rule
- is_stale: staleness helper
- DataQualityMetrics: counter increments, Prometheus labels, no forbidden labels
- Determinism: same inputs produce same outputs
"""

from __future__ import annotations

import pytest

from grinder.data.quality import (
    DataQualityConfig,
    GapDetector,
    OutlierFilter,
    is_stale,
)
from grinder.data.quality_metrics import (
    METRIC_DATA_GAP,
    METRIC_DATA_OUTLIER,
    METRIC_DATA_STALE,
    DataQualityMetrics,
    get_data_quality_metrics,
    reset_data_quality_metrics,
)
from grinder.observability.metrics_contract import FORBIDDEN_METRIC_LABELS


@pytest.fixture(autouse=True)
def _reset_dq_metrics() -> None:
    """Reset data quality metrics before each test."""
    reset_data_quality_metrics()


# ---------------------------------------------------------------------------
# GapDetector
# ---------------------------------------------------------------------------


class TestGapDetector:
    """Tests for GapDetector."""

    def test_first_tick_returns_none(self) -> None:
        """First observe per stream must return None (no previous ts)."""
        det = GapDetector()
        assert det.observe("book_ticker", ts_ms=1000) is None

    def test_no_gap_below_threshold(self) -> None:
        """Gap below smallest bucket returns None."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000, 5000))
        det = GapDetector(cfg)
        det.observe("book_ticker", ts_ms=1000)
        assert det.observe("book_ticker", ts_ms=1400) is None  # 400ms < 500

    def test_gap_in_first_bucket(self) -> None:
        """Gap matching first bucket returns event with correct bucket label."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000, 5000))
        det = GapDetector(cfg)
        det.observe("book_ticker", ts_ms=1000)
        event = det.observe("book_ticker", ts_ms=1600)  # 600ms >= 500, < 2000
        assert event is not None
        assert event.stream == "book_ticker"
        assert event.gap_ms == 600
        assert event.bucket == "500"

    def test_gap_in_middle_bucket(self) -> None:
        """Gap matching middle bucket returns correct bucket label."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000, 5000))
        det = GapDetector(cfg)
        det.observe("book_ticker", ts_ms=1000)
        event = det.observe("book_ticker", ts_ms=4000)  # 3000ms >= 2000, < 5000
        assert event is not None
        assert event.bucket == "2000"
        assert event.gap_ms == 3000

    def test_gap_in_overflow_bucket(self) -> None:
        """Gap exceeding all buckets returns '>max' bucket label."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000, 5000))
        det = GapDetector(cfg)
        det.observe("book_ticker", ts_ms=1000)
        event = det.observe("book_ticker", ts_ms=7000)  # 6000ms >= 5000
        assert event is not None
        assert event.bucket == ">5000"
        assert event.gap_ms == 6000

    def test_per_stream_isolation(self) -> None:
        """Different streams maintain independent state."""
        det = GapDetector()
        det.observe("book_ticker", ts_ms=1000)
        det.observe("agg_trade", ts_ms=2000)

        # book_ticker second tick at ts=1100 (100ms gap) — no gap event
        assert det.observe("book_ticker", ts_ms=1100) is None

        # agg_trade first tick was at 2000; no gap for first tick per stream
        # already set, so second call:
        assert det.observe("agg_trade", ts_ms=2100) is None

    def test_out_of_order_tick_not_flagged(self) -> None:
        """Tick with ts earlier than last should not produce a gap event."""
        det = GapDetector()
        det.observe("book_ticker", ts_ms=5000)
        assert det.observe("book_ticker", ts_ms=4000) is None

    def test_deterministic(self) -> None:
        """Same input sequence produces same output across two instances."""
        cfg = DataQualityConfig(gap_buckets_ms=(500, 2000))
        timestamps = [1000, 1200, 2500, 2600, 5000]

        results = []
        for _ in range(2):
            det = GapDetector(cfg)
            run_results = []
            for ts in timestamps:
                event = det.observe("bt", ts_ms=ts)
                run_results.append(event)
            results.append(run_results)

        assert results[0] == results[1]

    def test_reset_clears_state(self) -> None:
        """After reset, next observe returns None (as if first tick)."""
        det = GapDetector()
        det.observe("book_ticker", ts_ms=1000)
        det.reset()
        assert det.observe("book_ticker", ts_ms=9999) is None


# ---------------------------------------------------------------------------
# OutlierFilter
# ---------------------------------------------------------------------------


class TestOutlierFilter:
    """Tests for OutlierFilter."""

    def test_first_price_returns_none(self) -> None:
        """First observe_price per stream must return None."""
        filt = OutlierFilter()
        assert filt.observe_price("book_ticker", price=50000.0) is None

    def test_no_outlier_within_threshold(self) -> None:
        """Price jump within threshold returns None."""
        cfg = DataQualityConfig(price_jump_max_bps=500)  # 5%
        filt = OutlierFilter(cfg)
        filt.observe_price("book_ticker", price=50000.0)
        # 1% jump = 100 bps < 500 bps threshold
        assert filt.observe_price("book_ticker", price=50500.0) is None

    def test_outlier_exceeds_threshold(self) -> None:
        """Price jump exceeding threshold returns OutlierEvent."""
        cfg = DataQualityConfig(price_jump_max_bps=500)
        filt = OutlierFilter(cfg)
        filt.observe_price("book_ticker", price=50000.0)
        # 10% jump = 1000 bps > 500 bps
        event = filt.observe_price("book_ticker", price=55000.0)
        assert event is not None
        assert event.stream == "book_ticker"
        assert event.kind == "price"
        assert event.delta_bps == pytest.approx(1000.0)

    def test_exact_threshold_not_outlier(self) -> None:
        """Price jump exactly at threshold is NOT an outlier (strict >)."""
        cfg = DataQualityConfig(price_jump_max_bps=500)
        filt = OutlierFilter(cfg)
        filt.observe_price("book_ticker", price=10000.0)
        # Exactly 500 bps = 5% of 10000 = 500 → price = 10500
        assert filt.observe_price("book_ticker", price=10500.0) is None

    def test_last_price_always_updated(self) -> None:
        """last_price updates even after outlier, so detector doesn't stick."""
        cfg = DataQualityConfig(price_jump_max_bps=100)  # 1%
        filt = OutlierFilter(cfg)
        filt.observe_price("book_ticker", price=100.0)

        # 50% jump — outlier
        event = filt.observe_price("book_ticker", price=150.0)
        assert event is not None

        # Next tick: 150 → 151 = 0.67% = 67 bps < 100 — no outlier
        assert filt.observe_price("book_ticker", price=151.0) is None

    def test_zero_previous_price_no_crash(self) -> None:
        """If previous price was 0, skip bps calculation (no division by zero)."""
        filt = OutlierFilter()
        filt.observe_price("book_ticker", price=0.0)
        assert filt.observe_price("book_ticker", price=50000.0) is None

    def test_per_stream_isolation(self) -> None:
        """Different streams maintain independent price history."""
        cfg = DataQualityConfig(price_jump_max_bps=100)
        filt = OutlierFilter(cfg)
        filt.observe_price("book_ticker", price=100.0)
        filt.observe_price("agg_trade", price=200.0)

        # book_ticker: 100 → 101 = 1% = 100 bps — exact threshold, no outlier
        assert filt.observe_price("book_ticker", price=101.0) is None
        # agg_trade: 200 → 201 = 0.5% = 50 bps — no outlier
        assert filt.observe_price("agg_trade", price=201.0) is None

    def test_reset_clears_state(self) -> None:
        """After reset, next observe_price returns None."""
        filt = OutlierFilter()
        filt.observe_price("book_ticker", price=100.0)
        filt.reset()
        assert filt.observe_price("book_ticker", price=999.0) is None


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    """Tests for is_stale helper."""

    def test_not_stale_within_threshold(self) -> None:
        assert is_stale(now_ms=5000, last_ts_ms=4000, threshold_ms=2000) is False

    def test_stale_at_threshold(self) -> None:
        """Exactly at threshold counts as stale (>=)."""
        assert is_stale(now_ms=5000, last_ts_ms=3000, threshold_ms=2000) is True

    def test_stale_beyond_threshold(self) -> None:
        assert is_stale(now_ms=10000, last_ts_ms=1000, threshold_ms=2000) is True


# ---------------------------------------------------------------------------
# DataQualityConfig
# ---------------------------------------------------------------------------


class TestDataQualityConfig:
    """Tests for DataQualityConfig defaults."""

    def test_defaults(self) -> None:
        """Default config matches conservative SSOT values."""
        cfg = DataQualityConfig()
        assert cfg.dq_enabled is False
        assert cfg.stale_book_ticker_ms == 2000
        assert cfg.stale_depth_ms == 3000
        assert cfg.stale_agg_trade_ms == 5000
        assert cfg.gap_buckets_ms == (500, 2000, 5000)
        assert cfg.price_jump_max_bps == 500

    def test_frozen(self) -> None:
        """Config is immutable."""
        cfg = DataQualityConfig()
        with pytest.raises(AttributeError):
            cfg.dq_enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DataQualityMetrics
# ---------------------------------------------------------------------------


class TestDataQualityMetrics:
    """Tests for DataQualityMetrics."""

    def test_inc_stale(self) -> None:
        m = DataQualityMetrics()
        m.inc_stale("book_ticker")
        m.inc_stale("book_ticker")
        m.inc_stale("depth")
        assert m.stale_counts == {"book_ticker": 2, "depth": 1}

    def test_inc_gap(self) -> None:
        m = DataQualityMetrics()
        m.inc_gap("book_ticker", "500")
        m.inc_gap("book_ticker", "500")
        m.inc_gap("book_ticker", ">5000")
        assert m.gap_counts == {
            ("book_ticker", "500"): 2,
            ("book_ticker", ">5000"): 1,
        }

    def test_inc_outlier(self) -> None:
        m = DataQualityMetrics()
        m.inc_outlier("book_ticker", "price")
        assert m.outlier_counts == {("book_ticker", "price"): 1}

    def test_prometheus_lines_with_data(self) -> None:
        """Prometheus output has correct format when counters have data."""
        m = DataQualityMetrics()
        m.inc_stale("book_ticker")
        m.inc_gap("agg_trade", "2000")
        m.inc_outlier("book_ticker", "price")

        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        assert f"# HELP {METRIC_DATA_STALE}" in text
        assert f"# TYPE {METRIC_DATA_STALE} counter" in text
        assert f'{METRIC_DATA_STALE}{{stream="book_ticker"}} 1' in text

        assert f"# HELP {METRIC_DATA_GAP}" in text
        assert f'{METRIC_DATA_GAP}{{stream="agg_trade",bucket="2000"}} 1' in text

        assert f"# HELP {METRIC_DATA_OUTLIER}" in text
        assert f'{METRIC_DATA_OUTLIER}{{stream="book_ticker",kind="price"}} 1' in text

    def test_prometheus_lines_empty_defaults(self) -> None:
        """Empty metrics still emit HELP/TYPE + zero-value series."""
        m = DataQualityMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)

        assert f"# HELP {METRIC_DATA_STALE}" in text
        assert f"# TYPE {METRIC_DATA_STALE} counter" in text
        assert f'{METRIC_DATA_STALE}{{stream="none"}} 0' in text

        assert f'{METRIC_DATA_GAP}{{stream="none",bucket="none"}} 0' in text
        assert f'{METRIC_DATA_OUTLIER}{{stream="none",kind="none"}} 0' in text

    def test_no_forbidden_labels_in_output(self) -> None:
        """Data quality metrics must NOT contain any forbidden labels."""
        m = DataQualityMetrics()
        m.inc_stale("BTCUSDT_book_ticker")
        m.inc_gap("BTCUSDT_agg_trade", "500")
        m.inc_outlier("BTCUSDT_book_ticker", "price")

        text = "\n".join(m.to_prometheus_lines())

        for label in FORBIDDEN_METRIC_LABELS:
            assert label not in text, f"Forbidden label {label!r} found in metrics output"

    def test_singleton_pattern(self) -> None:
        """get_data_quality_metrics returns same instance."""
        m1 = get_data_quality_metrics()
        m2 = get_data_quality_metrics()
        assert m1 is m2

    def test_reset_clears_counters(self) -> None:
        m = DataQualityMetrics()
        m.inc_stale("x")
        m.inc_gap("x", "500")
        m.inc_outlier("x", "price")
        m.reset()
        assert m.stale_counts == {}
        assert m.gap_counts == {}
        assert m.outlier_counts == {}
