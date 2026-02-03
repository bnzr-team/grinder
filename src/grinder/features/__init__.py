"""Feature engine module for computing market features.

Provides:
- FeatureEngine: Orchestrates bar building and feature computation
- FeatureEngineConfig: Configuration for the feature engine
- FeatureSnapshot: Computed features at a point in time
- MidBar: OHLC bar built from mid-price ticks
- BarBuilder: Builds bars from tick stream
- BarBuilderConfig: Configuration for bar building

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5
"""

from grinder.features.bar import BarBuilder, BarBuilderConfig, MidBar
from grinder.features.engine import FeatureEngine, FeatureEngineConfig
from grinder.features.indicators import (
    compute_atr,
    compute_imbalance_l1_bps,
    compute_natr_bps,
    compute_range_trend,
    compute_thin_l1,
    compute_true_range,
)
from grinder.features.types import FeatureSnapshot

__all__ = [
    "BarBuilder",
    "BarBuilderConfig",
    "FeatureEngine",
    "FeatureEngineConfig",
    "FeatureSnapshot",
    "MidBar",
    "compute_atr",
    "compute_imbalance_l1_bps",
    "compute_natr_bps",
    "compute_range_trend",
    "compute_thin_l1",
    "compute_true_range",
]
