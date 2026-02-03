"""Feature types for the feature engine.

Provides FeatureSnapshot - a frozen dataclass containing all computed features
for a symbol at a point in time.

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.5
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FeatureSnapshot:
    """Computed features for a symbol at a point in time.

    All integer fields use basis points (bps) for determinism.
    Decimal fields are serialized as strings for precision.

    Attributes:
        ts: Timestamp when computed (ms)
        symbol: Trading symbol

        # L1 microstructure (§17.5.3)
        mid_price: Mid price (bid + ask) / 2
        spread_bps: Bid-ask spread in integer bps
        imbalance_l1_bps: L1 imbalance in integer bps [-10000, 10000]
        thin_l1: Thin side depth = min(bid_qty, ask_qty)

        # Volatility (§17.5.2)
        natr_bps: Normalized ATR in integer bps (0 if warmup)
        atr: Raw ATR as Decimal (None if warmup)

        # Range/trend (§17.5.5)
        sum_abs_returns_bps: Sum of absolute returns in bps
        net_return_bps: Net return over horizon in bps
        range_score: sum_abs / (net_return + 1) - higher = more choppy

        # Metadata
        warmup_bars: Number of completed bars available
    """

    ts: int
    symbol: str

    # L1 microstructure
    mid_price: Decimal
    spread_bps: int
    imbalance_l1_bps: int
    thin_l1: Decimal

    # Volatility
    natr_bps: int
    atr: Decimal | None

    # Range/trend
    sum_abs_returns_bps: int
    net_return_bps: int
    range_score: int

    # Metadata
    warmup_bars: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "mid_price": str(self.mid_price),
            "spread_bps": self.spread_bps,
            "imbalance_l1_bps": self.imbalance_l1_bps,
            "thin_l1": str(self.thin_l1),
            "natr_bps": self.natr_bps,
            "atr": str(self.atr) if self.atr is not None else None,
            "sum_abs_returns_bps": self.sum_abs_returns_bps,
            "net_return_bps": self.net_return_bps,
            "range_score": self.range_score,
            "warmup_bars": self.warmup_bars,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FeatureSnapshot:
        """Create from dict."""
        return cls(
            ts=d["ts"],
            symbol=d["symbol"],
            mid_price=Decimal(d["mid_price"]),
            spread_bps=d["spread_bps"],
            imbalance_l1_bps=d["imbalance_l1_bps"],
            thin_l1=Decimal(d["thin_l1"]),
            natr_bps=d["natr_bps"],
            atr=Decimal(d["atr"]) if d.get("atr") is not None else None,
            sum_abs_returns_bps=d["sum_abs_returns_bps"],
            net_return_bps=d["net_return_bps"],
            range_score=d["range_score"],
            warmup_bars=d["warmup_bars"],
        )

    def to_policy_features(self) -> dict[str, Any]:
        """Convert to dict for policy evaluation.

        Returns features as a flat dict that can be passed to
        GridPolicy.evaluate(). Includes mid_price for backward compat.
        """
        return {
            "mid_price": self.mid_price,
            "spread_bps": self.spread_bps,
            "imbalance_l1_bps": self.imbalance_l1_bps,
            "thin_l1": self.thin_l1,
            "natr_bps": self.natr_bps,
            "sum_abs_returns_bps": self.sum_abs_returns_bps,
            "net_return_bps": self.net_return_bps,
            "range_score": self.range_score,
            "warmup_bars": self.warmup_bars,
        }

    @property
    def is_warmed_up(self) -> bool:
        """Check if feature engine has enough bars for reliable features.

        Returns True if we have at least 15 bars (ATR(14) + 1).
        """
        return self.warmup_bars >= 15
