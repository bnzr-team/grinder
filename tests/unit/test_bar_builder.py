"""Tests for MidBar and BarBuilder (bar.py).

Tests verify:
- Bar timestamp alignment to interval boundaries
- OHLC tracking during bar accumulation
- Bar completion on boundary crossing
- Determinism (same ticks → same bars)

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5.1
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.features.bar import BarBuilder, BarBuilderConfig, MidBar


class TestMidBar:
    """Tests for MidBar frozen dataclass."""

    def test_midbar_is_frozen(self) -> None:
        """Test MidBar is immutable."""
        bar = MidBar(
            bar_ts=60_000,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("102"),
            tick_count=10,
        )

        with pytest.raises(AttributeError):
            bar.close = Decimal("999")  # type: ignore[misc]

    def test_midbar_to_dict(self) -> None:
        """Test MidBar serialization."""
        bar = MidBar(
            bar_ts=60_000,
            open=Decimal("100.5"),
            high=Decimal("105.25"),
            low=Decimal("95.75"),
            close=Decimal("102.125"),
            tick_count=5,
        )

        d = bar.to_dict()

        assert d["bar_ts"] == 60_000
        assert d["open"] == "100.5"
        assert d["high"] == "105.25"
        assert d["low"] == "95.75"
        assert d["close"] == "102.125"
        assert d["tick_count"] == 5

    def test_midbar_from_dict_roundtrip(self) -> None:
        """Test MidBar deserialization roundtrip."""
        original = MidBar(
            bar_ts=120_000,
            open=Decimal("50000.00"),
            high=Decimal("50100.50"),
            low=Decimal("49900.25"),
            close=Decimal("50050.75"),
            tick_count=100,
        )

        d = original.to_dict()
        restored = MidBar.from_dict(d)

        assert restored == original


class TestBarBuilderConfig:
    """Tests for BarBuilderConfig validation."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = BarBuilderConfig()

        assert config.bar_interval_ms == 60_000
        assert config.max_bars == 1000

    def test_custom_config(self) -> None:
        """Test custom configuration."""
        config = BarBuilderConfig(bar_interval_ms=5000, max_bars=500)

        assert config.bar_interval_ms == 5000
        assert config.max_bars == 500

    def test_invalid_bar_interval(self) -> None:
        """Test zero/negative bar_interval_ms raises ValueError."""
        with pytest.raises(ValueError, match="bar_interval_ms must be positive"):
            BarBuilderConfig(bar_interval_ms=0)

        with pytest.raises(ValueError, match="bar_interval_ms must be positive"):
            BarBuilderConfig(bar_interval_ms=-1000)

    def test_invalid_max_bars(self) -> None:
        """Test zero/negative max_bars raises ValueError."""
        with pytest.raises(ValueError, match="max_bars must be positive"):
            BarBuilderConfig(max_bars=0)


class TestBarAlignment:
    """Tests for bar timestamp alignment."""

    def test_bar_alignment_floor(self) -> None:
        """Test timestamps align to bar boundaries via floor division."""
        config = BarBuilderConfig(bar_interval_ms=60_000)  # 1 minute
        builder = BarBuilder(config=config)

        # Tick at 90 seconds should align to 60 second bar
        builder.process_tick(ts=90_000, mid_price=Decimal("100"))
        # Tick at 119 seconds still in same bar
        builder.process_tick(ts=119_000, mid_price=Decimal("101"))

        # No completed bar yet
        assert builder.bar_count == 0

        # Tick at 120 seconds crosses to new bar
        completed = builder.process_tick(ts=120_000, mid_price=Decimal("102"))

        assert completed is not None
        assert completed.bar_ts == 60_000  # Aligned to boundary

    def test_bar_alignment_exact_boundary(self) -> None:
        """Test tick at exact boundary starts new bar."""
        config = BarBuilderConfig(bar_interval_ms=60_000)
        builder = BarBuilder(config=config)

        # First tick at exact boundary
        builder.process_tick(ts=60_000, mid_price=Decimal("100"))
        # Another tick in same bar
        builder.process_tick(ts=60_001, mid_price=Decimal("101"))

        # Cross to next bar at exactly 120000
        completed = builder.process_tick(ts=120_000, mid_price=Decimal("102"))

        assert completed is not None
        assert completed.bar_ts == 60_000

    def test_bar_alignment_various_intervals(self) -> None:
        """Test alignment works for various bar intervals."""
        for interval_ms in [1000, 5000, 60_000, 300_000]:
            config = BarBuilderConfig(bar_interval_ms=interval_ms)
            builder = BarBuilder(config=config)

            # Start at timestamp just after boundary
            start_ts = interval_ms + 1
            builder.process_tick(ts=start_ts, mid_price=Decimal("100"))

            # Cross to next bar
            next_bar_ts = interval_ms * 2
            completed = builder.process_tick(ts=next_bar_ts, mid_price=Decimal("101"))

            assert completed is not None
            assert completed.bar_ts == interval_ms


class TestOhlcTracking:
    """Tests for OHLC value tracking during bar accumulation."""

    def test_ohlc_single_tick(self) -> None:
        """Test OHLC with single tick has same OHLC values."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        price = Decimal("50000")
        builder.process_tick(ts=0, mid_price=price)

        # Force bar completion
        completed = builder.process_tick(ts=60_000, mid_price=Decimal("50001"))

        assert completed is not None
        assert completed.open == price
        assert completed.high == price
        assert completed.low == price
        assert completed.close == price
        assert completed.tick_count == 1

    def test_ohlc_ascending_ticks(self) -> None:
        """Test OHLC with ascending price ticks."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # Ascending prices: 100, 101, 102, 103
        builder.process_tick(ts=0, mid_price=Decimal("100"))
        builder.process_tick(ts=10_000, mid_price=Decimal("101"))
        builder.process_tick(ts=20_000, mid_price=Decimal("102"))
        builder.process_tick(ts=30_000, mid_price=Decimal("103"))

        # Complete bar
        completed = builder.process_tick(ts=60_000, mid_price=Decimal("999"))

        assert completed is not None
        assert completed.open == Decimal("100")  # First tick
        assert completed.high == Decimal("103")  # Highest
        assert completed.low == Decimal("100")  # Lowest (first)
        assert completed.close == Decimal("103")  # Last tick in bar
        assert completed.tick_count == 4

    def test_ohlc_descending_ticks(self) -> None:
        """Test OHLC with descending price ticks."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # Descending prices: 103, 102, 101, 100
        builder.process_tick(ts=0, mid_price=Decimal("103"))
        builder.process_tick(ts=10_000, mid_price=Decimal("102"))
        builder.process_tick(ts=20_000, mid_price=Decimal("101"))
        builder.process_tick(ts=30_000, mid_price=Decimal("100"))

        completed = builder.process_tick(ts=60_000, mid_price=Decimal("999"))

        assert completed is not None
        assert completed.open == Decimal("103")
        assert completed.high == Decimal("103")  # First was highest
        assert completed.low == Decimal("100")  # Last was lowest
        assert completed.close == Decimal("100")

    def test_ohlc_volatile_ticks(self) -> None:
        """Test OHLC with volatile (up and down) price movement."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # Up, down, up, down pattern
        builder.process_tick(ts=0, mid_price=Decimal("100"))  # Open
        builder.process_tick(ts=10_000, mid_price=Decimal("110"))  # Up
        builder.process_tick(ts=20_000, mid_price=Decimal("95"))  # Down (new low)
        builder.process_tick(ts=30_000, mid_price=Decimal("115"))  # Up (new high)
        builder.process_tick(ts=40_000, mid_price=Decimal("105"))  # Close

        completed = builder.process_tick(ts=60_000, mid_price=Decimal("999"))

        assert completed is not None
        assert completed.open == Decimal("100")
        assert completed.high == Decimal("115")
        assert completed.low == Decimal("95")
        assert completed.close == Decimal("105")
        assert completed.tick_count == 5


class TestBarCompletion:
    """Tests for bar completion on boundary crossing."""

    def test_first_tick_no_completion(self) -> None:
        """Test first tick returns None (no completed bar)."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        result = builder.process_tick(ts=0, mid_price=Decimal("100"))

        assert result is None
        assert builder.bar_count == 0

    def test_same_bar_ticks_no_completion(self) -> None:
        """Test ticks within same bar don't complete."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # All ticks within [0, 60000)
        for ts in range(0, 60_000, 10_000):
            result = builder.process_tick(ts=ts, mid_price=Decimal("100"))
            assert result is None

        assert builder.bar_count == 0

    def test_boundary_crossing_completes_bar(self) -> None:
        """Test crossing bar boundary completes previous bar."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        builder.process_tick(ts=0, mid_price=Decimal("100"))
        builder.process_tick(ts=30_000, mid_price=Decimal("101"))

        # Cross boundary
        completed = builder.process_tick(ts=60_000, mid_price=Decimal("102"))

        assert completed is not None
        assert builder.bar_count == 1

    def test_multiple_bar_completions(self) -> None:
        """Test completing multiple bars sequentially."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # Bar 0: [0, 60000)
        builder.process_tick(ts=0, mid_price=Decimal("100"))
        bar1 = builder.process_tick(ts=60_000, mid_price=Decimal("101"))

        # Bar 1: [60000, 120000)
        bar2 = builder.process_tick(ts=120_000, mid_price=Decimal("102"))

        # Bar 2: [120000, 180000)
        bar3 = builder.process_tick(ts=180_000, mid_price=Decimal("103"))

        assert bar1 is not None
        assert bar2 is not None
        assert bar3 is not None

        assert bar1.bar_ts == 0
        assert bar2.bar_ts == 60_000
        assert bar3.bar_ts == 120_000

        assert builder.bar_count == 3

    def test_gap_in_ticks_single_bar(self) -> None:
        """Test gaps in ticks still produce single bar per interval."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # Tick at 0, then big gap to 180000 (3 minutes later)
        builder.process_tick(ts=0, mid_price=Decimal("100"))
        completed = builder.process_tick(ts=180_000, mid_price=Decimal("101"))

        # Only one bar completed (bar 0)
        assert completed is not None
        assert completed.bar_ts == 0
        assert builder.bar_count == 1

    def test_completed_bars_stored(self) -> None:
        """Test completed bars are retrievable via get_bars()."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        builder.process_tick(ts=0, mid_price=Decimal("100"))
        builder.process_tick(ts=60_000, mid_price=Decimal("110"))
        builder.process_tick(ts=120_000, mid_price=Decimal("120"))

        bars = builder.get_bars()

        assert len(bars) == 2
        assert bars[0].bar_ts == 0
        assert bars[0].open == Decimal("100")
        assert bars[1].bar_ts == 60_000
        assert bars[1].open == Decimal("110")


class TestBarBuilderMaxBars:
    """Tests for max_bars limit on bar storage."""

    def test_max_bars_limit(self) -> None:
        """Test bar deque respects maxlen."""
        config = BarBuilderConfig(bar_interval_ms=1000, max_bars=3)
        builder = BarBuilder(config=config)

        # Generate 5 bars
        for i in range(6):
            builder.process_tick(ts=i * 1000, mid_price=Decimal("100"))

        # Only last 3 bars kept
        bars = builder.get_bars()
        assert len(bars) == 3
        assert bars[0].bar_ts == 2000
        assert bars[1].bar_ts == 3000
        assert bars[2].bar_ts == 4000

    def test_get_bars_with_count(self) -> None:
        """Test get_bars with count limit."""
        config = BarBuilderConfig(bar_interval_ms=1000)
        builder = BarBuilder(config=config)

        # Generate 10 bars
        for i in range(11):
            builder.process_tick(ts=i * 1000, mid_price=Decimal("100"))

        # Get last 3 bars
        bars = builder.get_bars(count=3)

        assert len(bars) == 3
        assert bars[0].bar_ts == 7000
        assert bars[1].bar_ts == 8000
        assert bars[2].bar_ts == 9000


class TestBarBuilderReset:
    """Tests for BarBuilder reset functionality."""

    def test_reset_clears_state(self) -> None:
        """Test reset clears all accumulated state."""
        builder = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))

        # Accumulate some bars
        builder.process_tick(ts=0, mid_price=Decimal("100"))
        builder.process_tick(ts=60_000, mid_price=Decimal("101"))
        builder.process_tick(ts=120_000, mid_price=Decimal("102"))

        assert builder.bar_count == 2

        # Reset
        builder.reset()

        assert builder.bar_count == 0
        assert builder.get_bars() == []


class TestDeterminism:
    """Tests for deterministic bar building."""

    def test_same_ticks_same_bars(self) -> None:
        """Test identical tick sequences produce identical bars."""
        ticks = [
            (0, Decimal("100")),
            (10_000, Decimal("105")),
            (20_000, Decimal("95")),
            (30_000, Decimal("110")),
            (50_000, Decimal("102")),
            (60_000, Decimal("103")),
            (80_000, Decimal("108")),
            (120_000, Decimal("115")),
        ]

        # Run 1
        builder1 = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))
        for ts, price in ticks:
            builder1.process_tick(ts=ts, mid_price=price)
        bars1 = builder1.get_bars()

        # Run 2
        builder2 = BarBuilder(config=BarBuilderConfig(bar_interval_ms=60_000))
        for ts, price in ticks:
            builder2.process_tick(ts=ts, mid_price=price)
        bars2 = builder2.get_bars()

        # Must be identical
        assert len(bars1) == len(bars2)
        for b1, b2 in zip(bars1, bars2, strict=True):
            assert b1 == b2

    def test_order_matters(self) -> None:
        """Test that tick order affects OHLC (deterministic given order)."""
        config = BarBuilderConfig(bar_interval_ms=60_000)

        # Order 1: low first, then high
        builder1 = BarBuilder(config=config)
        builder1.process_tick(ts=0, mid_price=Decimal("90"))
        builder1.process_tick(ts=10_000, mid_price=Decimal("110"))
        bar1 = builder1.process_tick(ts=60_000, mid_price=Decimal("100"))

        # Order 2: high first, then low
        builder2 = BarBuilder(config=config)
        builder2.process_tick(ts=0, mid_price=Decimal("110"))
        builder2.process_tick(ts=10_000, mid_price=Decimal("90"))
        bar2 = builder2.process_tick(ts=60_000, mid_price=Decimal("100"))

        assert bar1 is not None
        assert bar2 is not None

        # High and low are same, but open and close differ
        assert bar1.high == bar2.high == Decimal("110")
        assert bar1.low == bar2.low == Decimal("90")
        assert bar1.open == Decimal("90")
        assert bar2.open == Decimal("110")
        assert bar1.close == Decimal("110")
        assert bar2.close == Decimal("90")
