"""Feature engine module for computing market features.

Provides:
- FeatureEngine: Orchestrates bar building and feature computation (v1)
- FeatureEngineConfig: Configuration for the feature engine
- FeatureSnapshot: Computed features at a point in time (v1)
- L2FeatureSnapshot: L2 order book features (v2)
- MidBar: OHLC bar built from mid-price ticks
- BarBuilder: Builds bars from tick stream
- BarBuilderConfig: Configuration for bar building

See:
- docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5 (FeatureEngine v1)
- docs/smart_grid/SPEC_V2_0.md §B (FeatureEngine v2 / L2 features)
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
from grinder.features.l2_indicators import (
    compute_depth_imbalance_bps,
    compute_depth_totals,
    compute_impact_buy_bps,
    compute_impact_sell_bps,
    compute_wall_score_x1000,
)
from grinder.features.l2_types import L2FeatureSnapshot
from grinder.features.types import FeatureSnapshot

__all__ = [
    "BarBuilder",
    "BarBuilderConfig",
    "FeatureEngine",
    "FeatureEngineConfig",
    "FeatureSnapshot",
    "L2FeatureSnapshot",
    "MidBar",
    "compute_atr",
    "compute_depth_imbalance_bps",
    "compute_depth_totals",
    "compute_imbalance_l1_bps",
    "compute_impact_buy_bps",
    "compute_impact_sell_bps",
    "compute_natr_bps",
    "compute_range_trend",
    "compute_thin_l1",
    "compute_true_range",
    "compute_wall_score_x1000",
]
