"""Tests for FeatureEngine (engine.py).

Tests verify:
- Snapshot processing and feature computation
- Warmup progression
- Multi-symbol isolation
- Determinism

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.contracts import Snapshot
from grinder.features import FeatureEngine, FeatureEngineConfig, FeatureSnapshot


def make_snapshot(
    ts: int = 0,
    symbol: str = "BTCUSDT",
    bid_price: str | Decimal = "50000",
    ask_price: str | Decimal = "50010",
    bid_qty: str | Decimal = "1.0",
    ask_qty: str | Decimal = "1.0",
    last_price: str | Decimal = "50005",
    last_qty: str | Decimal = "0.1",
) -> Snapshot:
    """Helper to create Snapshot with defaults."""
    return Snapshot(
        ts=ts,
        symbol=symbol,
        bid_price=Decimal(bid_price) if isinstance(bid_price, str) else bid_price,
        ask_price=Decimal(ask_price) if isinstance(ask_price, str) else ask_price,
        bid_qty=Decimal(bid_qty) if isinstance(bid_qty, str) else bid_qty,
        ask_qty=Decimal(ask_qty) if isinstance(ask_qty, str) else ask_qty,
        last_price=Decimal(last_price) if isinstance(last_price, str) else last_price,
        last_qty=Decimal(last_qty) if isinstance(last_qty, str) else last_qty,
    )


class TestFeatureEngineConfig:
    """Tests for FeatureEngineConfig."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = FeatureEngineConfig()

        assert config.bar_interval_ms == 60_000
        assert config.atr_period == 14
        assert config.range_horizon == 14
        assert config.max_bars == 1000

    def test_custom_config(self) -> None:
        """Test custom configuration."""
        config = FeatureEngineConfig(
            bar_interval_ms=5000,
            atr_period=7,
            range_horizon=10,
            max_bars=500,
        )

        assert config.bar_interval_ms == 5000
        assert config.atr_period == 7
        assert config.range_horizon == 10
        assert config.max_bars == 500

    def test_invalid_bar_interval(self) -> None:
        """Test validation rejects invalid bar_interval_ms."""
        with pytest.raises(ValueError, match="bar_interval_ms must be positive"):
            FeatureEngineConfig(bar_interval_ms=0)

    def test_invalid_atr_period(self) -> None:
        """Test validation rejects invalid atr_period."""
        with pytest.raises(ValueError, match="atr_period must be positive"):
            FeatureEngineConfig(atr_period=0)

    def test_invalid_range_horizon(self) -> None:
        """Test validation rejects invalid range_horizon."""
        with pytest.raises(ValueError, match="range_horizon must be positive"):
            FeatureEngineConfig(range_horizon=-1)

    def test_invalid_max_bars(self) -> None:
        """Test validation rejects invalid max_bars."""
        with pytest.raises(ValueError, match="max_bars must be positive"):
            FeatureEngineConfig(max_bars=0)


class TestProcessSnapshot:
    """Tests for process_snapshot functionality."""

    def test_process_snapshot_returns_feature_snapshot(self) -> None:
        """Test process_snapshot returns FeatureSnapshot."""
        engine = FeatureEngine()
        snapshot = make_snapshot(ts=0)

        result = engine.process_snapshot(snapshot)

        assert isinstance(result, FeatureSnapshot)

    def test_process_snapshot_copies_basic_fields(self) -> None:
        """Test basic fields are copied from snapshot."""
        engine = FeatureEngine()
        snapshot = make_snapshot(
            ts=12345,
            symbol="ETHUSDT",
            bid_price="3000",
            ask_price="3010",
        )

        result = engine.process_snapshot(snapshot)

        assert result.ts == 12345
        assert result.symbol == "ETHUSDT"
        assert result.mid_price == Decimal("3005")

    def test_process_snapshot_computes_spread_bps(self) -> None:
        """Test spread_bps is computed correctly."""
        engine = FeatureEngine()
        # bid=50000, ask=50050, mid=50025, spread=50
        # spread_bps = 50/50025 * 10000 ≈ 9.995
        snapshot = make_snapshot(bid_price="50000", ask_price="50050")

        result = engine.process_snapshot(snapshot)

        # spread_bps is truncated to int
        assert result.spread_bps == 9

    def test_process_snapshot_computes_imbalance(self) -> None:
        """Test imbalance_l1_bps is computed correctly."""
        engine = FeatureEngine()
        # bid_qty=3, ask_qty=1 → imbalance = (3-1)/(3+1) = 0.5 = 5000 bps
        snapshot = make_snapshot(bid_qty="3", ask_qty="1")

        result = engine.process_snapshot(snapshot)

        assert result.imbalance_l1_bps == 5000

    def test_process_snapshot_computes_thin_l1(self) -> None:
        """Test thin_l1 is computed correctly."""
        engine = FeatureEngine()
        snapshot = make_snapshot(bid_qty="5.5", ask_qty="3.2")

        result = engine.process_snapshot(snapshot)

        assert result.thin_l1 == Decimal("3.2")

    def test_process_snapshot_warmup_returns_zero_volatility(self) -> None:
        """Test volatility features return 0 during warmup."""
        engine = FeatureEngine()
        snapshot = make_snapshot(ts=0)

        result = engine.process_snapshot(snapshot)

        # No completed bars yet, so volatility is 0/None
        assert result.natr_bps == 0
        assert result.atr is None
        assert result.warmup_bars == 0


class TestWarmupProgression:
    """Tests for warmup progression."""

    def test_warmup_bars_increment(self) -> None:
        """Test warmup_bars increases as bars complete."""
        config = FeatureEngineConfig(bar_interval_ms=1000)  # 1 second bars
        engine = FeatureEngine(config=config)

        # Process snapshots across 5 bars
        for i in range(6):
            ts = i * 1000
            result = engine.process_snapshot(make_snapshot(ts=ts))

        # 5 completed bars (0-1s, 1-2s, 2-3s, 3-4s, 4-5s)
        assert result.warmup_bars == 5

    def test_warmup_progression_with_multiple_ticks_per_bar(self) -> None:
        """Test warmup with multiple ticks per bar."""
        config = FeatureEngineConfig(bar_interval_ms=1000)
        engine = FeatureEngine(config=config)

        # 10 ticks per bar, 3 bars
        for bar_idx in range(4):
            for tick_idx in range(10):
                ts = bar_idx * 1000 + tick_idx * 100
                result = engine.process_snapshot(make_snapshot(ts=ts))

        # 3 completed bars
        assert result.warmup_bars == 3

    def test_is_warmed_up_property(self) -> None:
        """Test is_warmed_up property threshold."""
        config = FeatureEngineConfig(bar_interval_ms=1000, atr_period=14)
        engine = FeatureEngine(config=config)

        # Process 14 bars - not warmed up yet (need 15 for ATR(14))
        for i in range(15):
            ts = i * 1000
            result = engine.process_snapshot(make_snapshot(ts=ts))

        assert result.warmup_bars == 14
        assert result.is_warmed_up is False

        # One more bar
        result = engine.process_snapshot(make_snapshot(ts=15000))
        assert result.warmup_bars == 15
        assert result.is_warmed_up is True


class TestVolatilityAfterWarmup:
    """Tests for volatility features after warmup."""

    def test_natr_computed_after_warmup(self) -> None:
        """Test NATR is computed once warmup is complete."""
        config = FeatureEngineConfig(bar_interval_ms=1000, atr_period=14)
        engine = FeatureEngine(config=config)

        # Generate 16 bars with consistent volatility
        for i in range(17):
            ts = i * 1000
            # Vary price within each bar to create TR
            for tick in range(3):
                tick_ts = ts + tick * 100
                price = 50000 + (tick * 10) - 10  # Creates range of 20
                result = engine.process_snapshot(
                    make_snapshot(
                        ts=tick_ts,
                        bid_price=str(price - 5),
                        ask_price=str(price + 5),
                    )
                )

        # Should have natr > 0 after warmup
        assert result.warmup_bars >= 15
        assert result.natr_bps > 0
        assert result.atr is not None


class TestRangeTrendAfterWarmup:
    """Tests for range/trend features after warmup."""

    def test_range_trend_computed_after_warmup(self) -> None:
        """Test range/trend features computed after warmup."""
        config = FeatureEngineConfig(bar_interval_ms=1000, range_horizon=5)
        engine = FeatureEngine(config=config)

        # Generate 7 bars with price trend
        prices = [50000, 50100, 50200, 50300, 50400, 50500, 50600]
        for i, price in enumerate(prices):
            ts = i * 1000
            result = engine.process_snapshot(
                make_snapshot(
                    ts=ts,
                    bid_price=str(price - 5),
                    ask_price=str(price + 5),
                )
            )

        # Should have range/trend values after horizon+1 bars
        assert result.sum_abs_returns_bps > 0
        assert result.net_return_bps > 0


class TestMultiSymbol:
    """Tests for multi-symbol isolation."""

    def test_symbols_isolated(self) -> None:
        """Test each symbol has independent state."""
        config = FeatureEngineConfig(bar_interval_ms=1000)
        engine = FeatureEngine(config=config)

        # Process 3 bars for BTC
        for i in range(4):
            engine.process_snapshot(make_snapshot(ts=i * 1000, symbol="BTCUSDT"))

        # Process 1 bar for ETH
        for i in range(2):
            engine.process_snapshot(make_snapshot(ts=i * 1000, symbol="ETHUSDT"))

        assert engine.get_bar_count("BTCUSDT") == 3
        assert engine.get_bar_count("ETHUSDT") == 1

    def test_get_all_symbols(self) -> None:
        """Test get_all_symbols returns all processed symbols."""
        engine = FeatureEngine()

        engine.process_snapshot(make_snapshot(symbol="BTCUSDT"))
        engine.process_snapshot(make_snapshot(symbol="ETHUSDT"))
        engine.process_snapshot(make_snapshot(symbol="SOLUSDT"))

        symbols = engine.get_all_symbols()

        assert set(symbols) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

    def test_reset_clears_all_symbols(self) -> None:
        """Test reset clears all symbol state."""
        engine = FeatureEngine()

        engine.process_snapshot(make_snapshot(symbol="BTCUSDT"))
        engine.process_snapshot(make_snapshot(symbol="ETHUSDT"))

        engine.reset()

        assert engine.get_all_symbols() == []
        assert engine.get_bar_count("BTCUSDT") == 0
        assert engine.get_bar_count("ETHUSDT") == 0

    def test_reset_symbol_clears_single_symbol(self) -> None:
        """Test reset_symbol clears only specified symbol."""
        config = FeatureEngineConfig(bar_interval_ms=1000)
        engine = FeatureEngine(config=config)

        # Process bars for both symbols
        for i in range(3):
            engine.process_snapshot(make_snapshot(ts=i * 1000, symbol="BTCUSDT"))
            engine.process_snapshot(make_snapshot(ts=i * 1000, symbol="ETHUSDT"))

        # Reset only BTC
        engine.reset_symbol("BTCUSDT")

        assert engine.get_bar_count("BTCUSDT") == 0
        assert engine.get_bar_count("ETHUSDT") == 2


class TestFeatureSnapshotSerialization:
    """Tests for FeatureSnapshot serialization."""

    def test_to_dict_roundtrip(self) -> None:
        """Test FeatureSnapshot to_dict/from_dict roundtrip."""
        config = FeatureEngineConfig(bar_interval_ms=1000)
        engine = FeatureEngine(config=config)

        # Generate enough bars for non-zero features
        for i in range(20):
            ts = i * 1000
            for tick in range(3):
                tick_ts = ts + tick * 100
                price = 50000 + (tick * 10)
                engine.process_snapshot(
                    make_snapshot(
                        ts=tick_ts,
                        bid_price=str(price - 5),
                        ask_price=str(price + 5),
                    )
                )

        # Get a feature snapshot
        original = engine.process_snapshot(make_snapshot(ts=20000))

        # Roundtrip
        d = original.to_dict()
        restored = FeatureSnapshot.from_dict(d)

        assert restored == original

    def test_to_policy_features(self) -> None:
        """Test to_policy_features returns correct dict."""
        engine = FeatureEngine()
        snapshot = make_snapshot(
            bid_price="50000",
            ask_price="50010",
            bid_qty="2",
            ask_qty="1",
        )

        result = engine.process_snapshot(snapshot)
        policy_features = result.to_policy_features()

        assert "mid_price" in policy_features
        assert "spread_bps" in policy_features
        assert "imbalance_l1_bps" in policy_features
        assert "thin_l1" in policy_features
        assert "natr_bps" in policy_features
        assert "warmup_bars" in policy_features

        assert policy_features["mid_price"] == Decimal("50005")
        assert policy_features["imbalance_l1_bps"] == 3333  # (2-1)/(2+1) ≈ 0.333


class TestDeterminism:
    """Tests for deterministic feature computation."""

    def test_same_snapshots_same_features(self) -> None:
        """Test identical snapshot sequences produce identical features."""
        snapshots = [
            make_snapshot(ts=i * 500, bid_price=str(50000 + (i % 10) * 5)) for i in range(50)
        ]

        config = FeatureEngineConfig(bar_interval_ms=1000)

        # Run 1
        engine1 = FeatureEngine(config=config)
        results1 = [engine1.process_snapshot(s) for s in snapshots]

        # Run 2
        engine2 = FeatureEngine(config=config)
        results2 = [engine2.process_snapshot(s) for s in snapshots]

        # Compare all results
        for r1, r2 in zip(results1, results2, strict=True):
            assert r1 == r2

    def test_determinism_with_gaps(self) -> None:
        """Test determinism with gaps in tick stream."""
        # Snapshots with irregular timing
        snapshots = [
            make_snapshot(ts=0),
            make_snapshot(ts=100),
            make_snapshot(ts=5000),  # Gap
            make_snapshot(ts=5100),
            make_snapshot(ts=10000),  # Gap
        ]

        config = FeatureEngineConfig(bar_interval_ms=1000)

        engine1 = FeatureEngine(config=config)
        engine2 = FeatureEngine(config=config)

        for s in snapshots:
            r1 = engine1.process_snapshot(s)
            r2 = engine2.process_snapshot(s)
            assert r1 == r2

    def test_determinism_multi_symbol(self) -> None:
        """Test determinism with multiple symbols interleaved."""
        snapshots = []
        for i in range(20):
            ts = i * 500
            snapshots.append(make_snapshot(ts=ts, symbol="BTCUSDT"))
            snapshots.append(make_snapshot(ts=ts, symbol="ETHUSDT"))

        config = FeatureEngineConfig(bar_interval_ms=1000)

        engine1 = FeatureEngine(config=config)
        engine2 = FeatureEngine(config=config)

        for s in snapshots:
            r1 = engine1.process_snapshot(s)
            r2 = engine2.process_snapshot(s)
            assert r1 == r2
