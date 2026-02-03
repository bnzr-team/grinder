"""Feature Engine for computing market features from snapshots.

Orchestrates bar building and feature computation for each symbol.
Maintains per-symbol state for deterministic replay.

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.features.bar import BarBuilder, BarBuilderConfig, MidBar
from grinder.features.indicators import (
    compute_atr,
    compute_imbalance_l1_bps,
    compute_natr_bps,
    compute_range_trend,
    compute_thin_l1,
)
from grinder.features.types import FeatureSnapshot

if TYPE_CHECKING:
    from grinder.contracts import Snapshot


@dataclass
class FeatureEngineConfig:
    """Configuration for FeatureEngine.

    Attributes:
        bar_interval_ms: Bar interval in milliseconds (default 60_000 = 1m)
        atr_period: Period for ATR/NATR calculation (default 14)
        range_horizon: Horizon for range/trend calculation (default 14)
        max_bars: Maximum bars to keep per symbol (default 1000)
    """

    bar_interval_ms: int = 60_000
    atr_period: int = 14
    range_horizon: int = 14
    max_bars: int = 1000

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.bar_interval_ms <= 0:
            raise ValueError(f"bar_interval_ms must be positive, got {self.bar_interval_ms}")
        if self.atr_period <= 0:
            raise ValueError(f"atr_period must be positive, got {self.atr_period}")
        if self.range_horizon <= 0:
            raise ValueError(f"range_horizon must be positive, got {self.range_horizon}")
        if self.max_bars <= 0:
            raise ValueError(f"max_bars must be positive, got {self.max_bars}")


@dataclass
class FeatureEngine:
    """Engine for computing market features from snapshot stream.

    Maintains per-symbol bar builders and computes features on each snapshot.
    All computations are deterministic for replay.

    Usage:
        engine = FeatureEngine(FeatureEngineConfig())
        for snapshot in snapshots:
            features = engine.process_snapshot(snapshot)
            # Use features.to_policy_features() for policy evaluation

    Thread-safety: No. Designed for single-threaded paper trading.
    Determinism: Yes. Same snapshot sequence produces identical features.
    """

    config: FeatureEngineConfig = field(default_factory=FeatureEngineConfig)

    # Per-symbol state
    _bar_builders: dict[str, BarBuilder] = field(default_factory=dict, repr=False)
    _bars: dict[str, deque[MidBar]] = field(default_factory=dict, repr=False)
    # Cache of latest FeatureSnapshot per symbol (for Top-K v1 selection)
    _latest_snapshots: dict[str, FeatureSnapshot] = field(default_factory=dict, repr=False)

    def _get_bar_builder(self, symbol: str) -> BarBuilder:
        """Get or create bar builder for symbol."""
        if symbol not in self._bar_builders:
            bar_config = BarBuilderConfig(
                bar_interval_ms=self.config.bar_interval_ms,
                max_bars=self.config.max_bars,
            )
            self._bar_builders[symbol] = BarBuilder(config=bar_config)
        return self._bar_builders[symbol]

    def _get_bars(self, symbol: str) -> deque[MidBar]:
        """Get or create bars deque for symbol."""
        if symbol not in self._bars:
            self._bars[symbol] = deque(maxlen=self.config.max_bars)
        return self._bars[symbol]

    def process_snapshot(self, snapshot: Snapshot) -> FeatureSnapshot:
        """Process a snapshot and compute features.

        Feeds the bar builder with the snapshot's mid price, then computes
        all features from the current bar history.

        Args:
            snapshot: Market data snapshot

        Returns:
            FeatureSnapshot with all computed features
        """
        symbol = snapshot.symbol
        mid_price = snapshot.mid_price

        # Feed bar builder
        bar_builder = self._get_bar_builder(symbol)
        completed_bar = bar_builder.process_tick(snapshot.ts, mid_price)

        # Store completed bar if any
        bars_deque = self._get_bars(symbol)
        if completed_bar is not None:
            bars_deque.append(completed_bar)

        # Get bar list for indicator computation
        bars = list(bars_deque)

        # Compute L1 features
        spread_bps = int(snapshot.spread_bps)
        imbalance_l1_bps = compute_imbalance_l1_bps(snapshot.bid_qty, snapshot.ask_qty)
        thin_l1 = compute_thin_l1(snapshot.bid_qty, snapshot.ask_qty)

        # Compute volatility features
        atr = compute_atr(bars, self.config.atr_period)
        natr_bps = compute_natr_bps(bars, self.config.atr_period)

        # Compute range/trend features
        sum_abs_bps, net_ret_bps, range_score = compute_range_trend(bars, self.config.range_horizon)

        feature_snapshot = FeatureSnapshot(
            ts=snapshot.ts,
            symbol=symbol,
            mid_price=mid_price,
            spread_bps=spread_bps,
            imbalance_l1_bps=imbalance_l1_bps,
            thin_l1=thin_l1,
            natr_bps=natr_bps,
            atr=atr,
            sum_abs_returns_bps=sum_abs_bps,
            net_return_bps=net_ret_bps,
            range_score=range_score,
            warmup_bars=len(bars),
        )

        # Cache latest snapshot for Top-K v1 selection
        self._latest_snapshots[symbol] = feature_snapshot

        return feature_snapshot

    def get_bar_count(self, symbol: str) -> int:
        """Get number of completed bars for a symbol."""
        if symbol in self._bars:
            return len(self._bars[symbol])
        return 0

    def get_all_symbols(self) -> list[str]:
        """Get all symbols that have been processed."""
        return list(self._bar_builders.keys())

    def get_latest_snapshot(self, symbol: str) -> FeatureSnapshot | None:
        """Get the latest feature snapshot for a symbol.

        Returns None if the symbol hasn't been processed yet.
        """
        return self._latest_snapshots.get(symbol)

    def get_all_latest_snapshots(self) -> dict[str, FeatureSnapshot]:
        """Get the latest feature snapshots for all symbols.

        Returns a dict mapping symbol to its latest FeatureSnapshot.
        Only includes symbols that have been processed at least once.
        """
        return dict(self._latest_snapshots)

    def reset(self) -> None:
        """Reset all state."""
        self._bar_builders.clear()
        self._bars.clear()
        self._latest_snapshots.clear()

    def reset_symbol(self, symbol: str) -> None:
        """Reset state for a single symbol."""
        if symbol in self._bar_builders:
            del self._bar_builders[symbol]
        if symbol in self._bars:
            del self._bars[symbol]
        if symbol in self._latest_snapshots:
            del self._latest_snapshots[symbol]
