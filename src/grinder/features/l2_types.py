"""L2 feature types for FeatureEngine v2.

Provides L2FeatureSnapshot - a frozen dataclass containing all L2-derived features
computed from an L2 order book snapshot.

Field names match SSOT contract in SPEC_V2_0.md §B.2 exactly (*_topN_* naming).

See: docs/smart_grid/SPEC_V2_0.md Addendum B
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from grinder.features.l2_indicators import (
    compute_depth_imbalance_bps,
    compute_depth_totals,
    compute_impact_buy_bps,
    compute_impact_sell_bps,
    compute_wall_score_x1000,
)
from grinder.replay.l2_snapshot import (
    IMPACT_INSUFFICIENT_DEPTH_BPS,
    QTY_REF_BASELINE,
    L2Snapshot,
)


@dataclass(frozen=True)
class L2FeatureSnapshot:
    """Computed L2 features for a symbol at a point in time.

    All integer fields use basis points (bps) or x1000 for determinism.
    Field names match SPEC_V2_0.md §B.2 exactly (*_topN_* naming).

    Attributes:
        ts_ms: Timestamp when snapshot was taken (ms)
        symbol: Trading symbol
        venue: Exchange venue
        depth: Number of levels per side (topN)

        # Depth features (§B.2)
        depth_bid_qty_topN: Total bid-side quantity across topN levels
        depth_ask_qty_topN: Total ask-side quantity across topN levels
        depth_imbalance_topN_bps: Bid-ask depth imbalance in bps [-10000, 10000]

        # Impact features (VWAP slippage) (§B.2, §B.3)
        impact_buy_topN_bps: Buy impact in bps (slippage from best ask)
        impact_sell_topN_bps: Sell impact in bps (slippage from best bid)
        impact_buy_topN_insufficient_depth: 1 if buy depth exhausted, 0 otherwise
        impact_sell_topN_insufficient_depth: 1 if sell depth exhausted, 0 otherwise

        # Wall features (§B.2, §B.4)
        wall_bid_score_topN_x1000: Bid wall score * 1000
        wall_ask_score_topN_x1000: Ask wall score * 1000

        # Config
        qty_ref: Reference quantity used for impact calculation
    """

    ts_ms: int
    symbol: str
    venue: str
    depth: int

    # Depth features
    depth_bid_qty_topN: Decimal
    depth_ask_qty_topN: Decimal
    depth_imbalance_topN_bps: int

    # Impact features
    impact_buy_topN_bps: int
    impact_sell_topN_bps: int
    impact_buy_topN_insufficient_depth: int
    impact_sell_topN_insufficient_depth: int

    # Wall features
    wall_bid_score_topN_x1000: int
    wall_ask_score_topN_x1000: int

    # Config
    qty_ref: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts_ms": self.ts_ms,
            "symbol": self.symbol,
            "venue": self.venue,
            "depth": self.depth,
            "depth_bid_qty_topN": str(self.depth_bid_qty_topN),
            "depth_ask_qty_topN": str(self.depth_ask_qty_topN),
            "depth_imbalance_topN_bps": self.depth_imbalance_topN_bps,
            "impact_buy_topN_bps": self.impact_buy_topN_bps,
            "impact_sell_topN_bps": self.impact_sell_topN_bps,
            "impact_buy_topN_insufficient_depth": self.impact_buy_topN_insufficient_depth,
            "impact_sell_topN_insufficient_depth": self.impact_sell_topN_insufficient_depth,
            "wall_bid_score_topN_x1000": self.wall_bid_score_topN_x1000,
            "wall_ask_score_topN_x1000": self.wall_ask_score_topN_x1000,
            "qty_ref": str(self.qty_ref),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> L2FeatureSnapshot:
        """Create from dict."""
        return cls(
            ts_ms=d["ts_ms"],
            symbol=d["symbol"],
            venue=d["venue"],
            depth=d["depth"],
            depth_bid_qty_topN=Decimal(d["depth_bid_qty_topN"]),
            depth_ask_qty_topN=Decimal(d["depth_ask_qty_topN"]),
            depth_imbalance_topN_bps=d["depth_imbalance_topN_bps"],
            impact_buy_topN_bps=d["impact_buy_topN_bps"],
            impact_sell_topN_bps=d["impact_sell_topN_bps"],
            impact_buy_topN_insufficient_depth=d["impact_buy_topN_insufficient_depth"],
            impact_sell_topN_insufficient_depth=d["impact_sell_topN_insufficient_depth"],
            wall_bid_score_topN_x1000=d["wall_bid_score_topN_x1000"],
            wall_ask_score_topN_x1000=d["wall_ask_score_topN_x1000"],
            qty_ref=Decimal(d["qty_ref"]),
        )

    @classmethod
    def from_l2_snapshot(
        cls,
        snapshot: L2Snapshot,
        qty_ref: Decimal = QTY_REF_BASELINE,
    ) -> L2FeatureSnapshot:
        """Compute L2 features from an L2Snapshot.

        Args:
            snapshot: L2 order book snapshot
            qty_ref: Reference quantity for impact calculation

        Returns:
            L2FeatureSnapshot with all features computed
        """
        # Depth totals
        depth_bid_qty, depth_ask_qty = compute_depth_totals(snapshot.bids, snapshot.asks)

        # Depth imbalance
        depth_imbalance_bps = compute_depth_imbalance_bps(snapshot.bids, snapshot.asks)

        # Impact
        impact_buy_bps = compute_impact_buy_bps(snapshot.asks, qty_ref)
        impact_sell_bps = compute_impact_sell_bps(snapshot.bids, qty_ref)

        # Insufficient depth flags
        impact_buy_insufficient = 1 if impact_buy_bps == IMPACT_INSUFFICIENT_DEPTH_BPS else 0
        impact_sell_insufficient = 1 if impact_sell_bps == IMPACT_INSUFFICIENT_DEPTH_BPS else 0

        # Wall scores
        wall_bid_score = compute_wall_score_x1000(snapshot.bids)
        wall_ask_score = compute_wall_score_x1000(snapshot.asks)

        return cls(
            ts_ms=snapshot.ts_ms,
            symbol=snapshot.symbol,
            venue=snapshot.venue,
            depth=snapshot.depth,
            depth_bid_qty_topN=depth_bid_qty,
            depth_ask_qty_topN=depth_ask_qty,
            depth_imbalance_topN_bps=depth_imbalance_bps,
            impact_buy_topN_bps=impact_buy_bps,
            impact_sell_topN_bps=impact_sell_bps,
            impact_buy_topN_insufficient_depth=impact_buy_insufficient,
            impact_sell_topN_insufficient_depth=impact_sell_insufficient,
            wall_bid_score_topN_x1000=wall_bid_score,
            wall_ask_score_topN_x1000=wall_ask_score,
            qty_ref=qty_ref,
        )
