"""Tests for technical indicators (indicators.py).

Tests verify:
- True Range calculation
- ATR (Average True Range) computation
- NATR (Normalized ATR) in integer bps
- L1 microstructure features (imbalance, thin side)
- Range/trend indicators

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5.2, §17.5.3, §17.5.5
"""

from __future__ import annotations

from decimal import Decimal

from grinder.features.bar import MidBar
from grinder.features.indicators import (
    compute_atr,
    compute_imbalance_l1_bps,
    compute_natr_bps,
    compute_range_trend,
    compute_thin_l1,
    compute_true_range,
)


def make_bar(
    bar_ts: int = 0,
    open_: Decimal | str = "100",
    high: Decimal | str = "100",
    low: Decimal | str = "100",
    close: Decimal | str = "100",
    tick_count: int = 1,
) -> MidBar:
    """Helper to create MidBar with defaults."""
    return MidBar(
        bar_ts=bar_ts,
        open=Decimal(open_) if isinstance(open_, str) else open_,
        high=Decimal(high) if isinstance(high, str) else high,
        low=Decimal(low) if isinstance(low, str) else low,
        close=Decimal(close) if isinstance(close, str) else close,
        tick_count=tick_count,
    )


class TestTrueRange:
    """Tests for compute_true_range."""

    def test_tr_basic_high_low(self) -> None:
        """Test TR when high-low is largest component."""
        bar = make_bar(high="110", low="90", close="100")
        prev_close = Decimal("100")

        tr = compute_true_range(bar, prev_close)

        # high - low = 110 - 90 = 20
        # |high - prev_close| = |110 - 100| = 10
        # |low - prev_close| = |90 - 100| = 10
        assert tr == Decimal("20")

    def test_tr_gap_up(self) -> None:
        """Test TR when price gapped up (high-prev_close dominant)."""
        bar = make_bar(high="120", low="115", close="118")
        prev_close = Decimal("100")

        tr = compute_true_range(bar, prev_close)

        # high - low = 120 - 115 = 5
        # |high - prev_close| = |120 - 100| = 20
        # |low - prev_close| = |115 - 100| = 15
        assert tr == Decimal("20")

    def test_tr_gap_down(self) -> None:
        """Test TR when price gapped down (low-prev_close dominant)."""
        bar = make_bar(high="85", low="80", close="82")
        prev_close = Decimal("100")

        tr = compute_true_range(bar, prev_close)

        # high - low = 85 - 80 = 5
        # |high - prev_close| = |85 - 100| = 15
        # |low - prev_close| = |80 - 100| = 20
        assert tr == Decimal("20")

    def test_tr_no_gap(self) -> None:
        """Test TR when prev_close is within bar range."""
        bar = make_bar(high="110", low="90", close="105")
        prev_close = Decimal("100")

        tr = compute_true_range(bar, prev_close)

        # high - low = 20, dominates since prev_close within range
        assert tr == Decimal("20")


class TestATR:
    """Tests for compute_atr."""

    def test_atr_insufficient_data(self) -> None:
        """Test ATR returns None with insufficient bars."""
        bars = [make_bar(bar_ts=i * 60000) for i in range(10)]

        # Need period+1 = 15 bars for ATR(14)
        atr = compute_atr(bars, period=14)

        assert atr is None

    def test_atr_minimum_data(self) -> None:
        """Test ATR with exact minimum bars (period+1)."""
        # Create 15 bars with known TR values
        # Each bar: high=105, low=95 (range=10), close=100
        bars = [make_bar(bar_ts=i * 60000, high="105", low="95", close="100") for i in range(15)]

        atr = compute_atr(bars, period=14)

        assert atr is not None
        # All TRs = 10 (since prev_close=100 is within range)
        # ATR = sum(10 * 14) / 14 = 10
        assert atr == Decimal("10")

    def test_atr_varying_ranges(self) -> None:
        """Test ATR with varying bar ranges."""
        bars = []
        # Bar 0: baseline
        bars.append(make_bar(bar_ts=0, high="100", low="100", close="100"))

        # Bars 1-14: alternating ranges
        for i in range(1, 15):
            if i % 2 == 0:
                # Even bars have range of 10
                bars.append(make_bar(bar_ts=i * 60000, high="105", low="95", close="100"))
            else:
                # Odd bars have range of 20
                bars.append(make_bar(bar_ts=i * 60000, high="110", low="90", close="100"))

        atr = compute_atr(bars, period=14)

        assert atr is not None
        # 7 bars with TR=10, 7 bars with TR=20 (bars 1-14)
        # ATR = (7*10 + 7*20) / 14 = 210/14 = 15
        assert atr == Decimal("15")

    def test_atr_uses_recent_bars(self) -> None:
        """Test ATR uses last N TRs (most recent)."""
        # Create 20 bars
        bars = []
        bars.append(make_bar(bar_ts=0, close="100"))

        # Bars 1-10: TR = 10
        for i in range(1, 11):
            bars.append(make_bar(bar_ts=i * 60000, high="105", low="95", close="100"))

        # Bars 11-19: TR = 20
        for i in range(11, 20):
            bars.append(make_bar(bar_ts=i * 60000, high="110", low="90", close="100"))

        atr = compute_atr(bars, period=14)

        assert atr is not None
        # Last 14 TRs: 5 from bars 6-10 (TR=10), 9 from bars 11-19 (TR=20)
        # ATR = (5*10 + 9*20) / 14 = 230/14 ≈ 16.43
        expected = (Decimal("5") * 10 + Decimal("9") * 20) / Decimal("14")
        assert atr == expected


class TestNATRBps:
    """Tests for compute_natr_bps."""

    def test_natr_insufficient_data(self) -> None:
        """Test NATR returns 0 with insufficient data."""
        bars = [make_bar(bar_ts=i * 60000) for i in range(10)]

        natr = compute_natr_bps(bars, period=14)

        assert natr == 0

    def test_natr_calculation(self) -> None:
        """Test NATR in integer bps."""
        # Create 15 bars with TR=10, close=100
        bars = [make_bar(bar_ts=i * 60000, high="105", low="95", close="100") for i in range(15)]

        natr = compute_natr_bps(bars, period=14)

        # ATR = 10, close = 100
        # NATR = 10 / 100 = 0.10 = 1000 bps
        assert natr == 1000

    def test_natr_high_volatility(self) -> None:
        """Test NATR with high volatility."""
        # Create 15 bars with TR=50, close=100
        bars = [make_bar(bar_ts=i * 60000, high="125", low="75", close="100") for i in range(15)]

        natr = compute_natr_bps(bars, period=14)

        # ATR = 50, close = 100
        # NATR = 50 / 100 = 0.50 = 5000 bps
        assert natr == 5000

    def test_natr_low_volatility(self) -> None:
        """Test NATR with low volatility."""
        # Create 15 bars with TR=1, close=100
        bars = [
            make_bar(bar_ts=i * 60000, high="100.5", low="99.5", close="100") for i in range(15)
        ]

        natr = compute_natr_bps(bars, period=14)

        # ATR = 1, close = 100
        # NATR = 1 / 100 = 0.01 = 100 bps
        assert natr == 100

    def test_natr_zero_close_returns_zero(self) -> None:
        """Test NATR returns 0 if close is 0 (avoid div by zero)."""
        bars = [make_bar(bar_ts=i * 60000, high="1", low="0", close="0") for i in range(15)]

        natr = compute_natr_bps(bars, period=14)

        assert natr == 0


class TestImbalanceL1Bps:
    """Tests for compute_imbalance_l1_bps."""

    def test_imbalance_balanced(self) -> None:
        """Test imbalance when bid == ask (balanced)."""
        bid_qty = Decimal("100")
        ask_qty = Decimal("100")

        imbalance = compute_imbalance_l1_bps(bid_qty, ask_qty)

        # (100 - 100) / (100 + 100 + eps) ≈ 0
        assert imbalance == 0

    def test_imbalance_bid_heavy(self) -> None:
        """Test positive imbalance when bid > ask."""
        bid_qty = Decimal("150")
        ask_qty = Decimal("50")

        imbalance = compute_imbalance_l1_bps(bid_qty, ask_qty)

        # (150 - 50) / (150 + 50 + eps) = 100/200 = 0.5 = 5000 bps
        assert imbalance == 5000

    def test_imbalance_ask_heavy(self) -> None:
        """Test negative imbalance when ask > bid."""
        bid_qty = Decimal("50")
        ask_qty = Decimal("150")

        imbalance = compute_imbalance_l1_bps(bid_qty, ask_qty)

        # (50 - 150) / (50 + 150 + eps) = -100/200 = -0.5 = -5000 bps
        assert imbalance == -5000

    def test_imbalance_extreme_bid(self) -> None:
        """Test extreme bid-side imbalance."""
        bid_qty = Decimal("1000")
        ask_qty = Decimal("1")

        imbalance = compute_imbalance_l1_bps(bid_qty, ask_qty)

        # (1000 - 1) / (1000 + 1 + eps) ≈ 999/1001 ≈ 0.998 ≈ 9980 bps
        assert imbalance >= 9970
        assert imbalance <= 10000

    def test_imbalance_extreme_ask(self) -> None:
        """Test extreme ask-side imbalance."""
        bid_qty = Decimal("1")
        ask_qty = Decimal("1000")

        imbalance = compute_imbalance_l1_bps(bid_qty, ask_qty)

        assert imbalance <= -9970
        assert imbalance >= -10000

    def test_imbalance_zero_quantities(self) -> None:
        """Test imbalance with zero quantities."""
        bid_qty = Decimal("0")
        ask_qty = Decimal("0")

        imbalance = compute_imbalance_l1_bps(bid_qty, ask_qty)

        # 0 / eps ≈ 0
        assert imbalance == 0


class TestThinL1:
    """Tests for compute_thin_l1."""

    def test_thin_l1_balanced(self) -> None:
        """Test thin_l1 when bid == ask."""
        bid_qty = Decimal("100")
        ask_qty = Decimal("100")

        thin = compute_thin_l1(bid_qty, ask_qty)

        assert thin == Decimal("100")

    def test_thin_l1_bid_thin(self) -> None:
        """Test thin_l1 when bid is thinner."""
        bid_qty = Decimal("50")
        ask_qty = Decimal("150")

        thin = compute_thin_l1(bid_qty, ask_qty)

        assert thin == Decimal("50")

    def test_thin_l1_ask_thin(self) -> None:
        """Test thin_l1 when ask is thinner."""
        bid_qty = Decimal("150")
        ask_qty = Decimal("50")

        thin = compute_thin_l1(bid_qty, ask_qty)

        assert thin == Decimal("50")

    def test_thin_l1_zero_bid(self) -> None:
        """Test thin_l1 with zero bid."""
        bid_qty = Decimal("0")
        ask_qty = Decimal("100")

        thin = compute_thin_l1(bid_qty, ask_qty)

        assert thin == Decimal("0")

    def test_thin_l1_zero_ask(self) -> None:
        """Test thin_l1 with zero ask."""
        bid_qty = Decimal("100")
        ask_qty = Decimal("0")

        thin = compute_thin_l1(bid_qty, ask_qty)

        assert thin == Decimal("0")


class TestRangeTrend:
    """Tests for compute_range_trend."""

    def test_range_trend_insufficient_data(self) -> None:
        """Test range_trend returns zeros with insufficient data."""
        bars = [make_bar(bar_ts=i * 60000) for i in range(10)]

        sum_abs, net_ret, range_score = compute_range_trend(bars, horizon=14)

        assert sum_abs == 0
        assert net_ret == 0
        assert range_score == 0

    def test_range_trend_trending_up(self) -> None:
        """Test range_score for trending (directional) market."""
        # Create 15 bars with steady uptrend
        # Each bar closes 1% higher than previous
        bars = []
        price = Decimal("100")
        for i in range(15):
            bars.append(make_bar(bar_ts=i * 60000, close=str(price)))
            price = price * Decimal("1.01")  # 1% increase

        sum_abs, net_ret, range_score = compute_range_trend(bars, horizon=14)

        assert sum_abs > 0
        assert net_ret > 0
        # Trending: range_score should be low (sum_abs ≈ net_ret)
        # sum_abs = 14 * 100 bps = 1400 bps
        # net_ret = 14.87% ≈ 1487 bps
        # range_score ≈ 1400 / (1487 + 1) ≈ 0-1
        assert range_score <= 2

    def test_range_trend_choppy(self) -> None:
        """Test range_score for choppy (ranging) market."""
        # Create 15 bars that oscillate but end near start
        bars = []
        prices = [100, 105, 95, 105, 95, 105, 95, 105, 95, 105, 95, 105, 95, 105, 100]
        for i, p in enumerate(prices):
            bars.append(make_bar(bar_ts=i * 60000, close=str(p)))

        sum_abs, net_ret, range_score = compute_range_trend(bars, horizon=14)

        assert sum_abs > 0
        assert net_ret == 0  # Back to start
        # Choppy: range_score should be high (lots of movement, no net progress)
        # range_score = sum_abs / (0 + 1) = sum_abs (which is large)
        assert range_score > 100  # High choppiness

    def test_range_trend_flat(self) -> None:
        """Test range_score for flat (no movement) market."""
        # Create 15 bars with same close price
        bars = [make_bar(bar_ts=i * 60000, close="100") for i in range(15)]

        sum_abs, net_ret, range_score = compute_range_trend(bars, horizon=14)

        assert sum_abs == 0
        assert net_ret == 0
        assert range_score == 0

    def test_range_trend_calculation_accuracy(self) -> None:
        """Test exact calculation of range_trend values."""
        # Create specific pattern
        bars = []
        # Bar 0: close = 100
        bars.append(make_bar(bar_ts=0, close="100"))
        # Bar 1: close = 110 (10% up)
        bars.append(make_bar(bar_ts=60000, close="110"))
        # Bar 2: close = 100 (9.09% down)
        bars.append(make_bar(bar_ts=120000, close="100"))

        sum_abs, net_ret, range_score = compute_range_trend(bars, horizon=2)

        # returns: |110-100|/100 = 10%, |100-110|/110 = 9.09%
        # sum_abs = 1000 + 909 = 1909 bps
        # net_ret = |100/100 - 1| = 0
        # range_score = 1909 / 1 = 1909
        assert 1900 <= sum_abs <= 1920
        assert net_ret == 0
        assert range_score == sum_abs  # sum_abs / (0+1)


class TestDeterminism:
    """Tests for deterministic indicator computation."""

    def test_atr_deterministic(self) -> None:
        """Test ATR computation is deterministic."""
        bars = [
            make_bar(bar_ts=i * 60000, high=str(100 + i), low=str(90 + i), close=str(95 + i))
            for i in range(20)
        ]

        atr1 = compute_atr(bars, period=14)
        atr2 = compute_atr(bars, period=14)

        assert atr1 == atr2

    def test_natr_deterministic(self) -> None:
        """Test NATR computation is deterministic."""
        bars = [
            make_bar(bar_ts=i * 60000, high=str(100 + i), low=str(90 + i), close=str(95 + i))
            for i in range(20)
        ]

        natr1 = compute_natr_bps(bars, period=14)
        natr2 = compute_natr_bps(bars, period=14)

        assert natr1 == natr2

    def test_range_trend_deterministic(self) -> None:
        """Test range_trend computation is deterministic."""
        bars = [make_bar(bar_ts=i * 60000, close=str(100 + (i % 5))) for i in range(20)]

        result1 = compute_range_trend(bars, horizon=14)
        result2 = compute_range_trend(bars, horizon=14)

        assert result1 == result2
