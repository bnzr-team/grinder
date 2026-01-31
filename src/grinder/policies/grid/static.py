"""Static symmetric grid policy (GridPolicy v0).

This is a minimal grid policy that produces deterministic, symmetric grids.
No adaptive behavior, no inventory skew, no regime changes.

See: docs/07_GRID_POLICY_LIBRARY.md section 2.1 (simplified for v0)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from grinder.core import GridMode, MarketRegime, ResetAction
from grinder.policies.base import GridPlan, GridPolicy


class StaticGridPolicy(GridPolicy):
    """Static symmetric grid policy.

    Produces bilateral grids with:
    - Fixed spacing (no volatility adjustment)
    - Equal levels on both sides (symmetric)
    - No inventory skew
    - No regime switching (always RANGE)
    - No auto-reset (always NONE)

    This is the simplest grid policy for M1 vertical slice.

    Limitations v0:
    - No toxicity gating (handled by prefilter)
    - No spread-based throttling
    - No inventory-based adjustments
    - Fixed size schedule (uniform)
    """

    name = "STATIC_GRID"

    def __init__(
        self,
        spacing_bps: float = 10.0,
        levels: int = 5,
        size_per_level: Decimal = Decimal("100"),
    ) -> None:
        """Initialize static grid policy.

        Args:
            spacing_bps: Grid spacing in basis points (default 10 bps)
            levels: Number of levels on each side (symmetric)
            size_per_level: Order size per level in quote currency (USD)
        """
        if spacing_bps <= 0:
            raise ValueError("spacing_bps must be positive")
        if levels <= 0:
            raise ValueError("levels must be positive")
        if size_per_level <= 0:
            raise ValueError("size_per_level must be positive")

        self.spacing_bps = spacing_bps
        self.levels = levels
        self.size_per_level = size_per_level

    def evaluate(self, features: dict[str, Any]) -> GridPlan:
        """Evaluate features and return static symmetric grid plan.

        Args:
            features: Dict containing at least:
                - mid_price: Current mid price (Decimal or float)

        Returns:
            GridPlan with static symmetric configuration

        Raises:
            KeyError: If mid_price is missing from features
        """
        mid_price = features.get("mid_price")
        if mid_price is None:
            raise KeyError("mid_price is required in features")

        # Convert to Decimal if needed
        if not isinstance(mid_price, Decimal):
            mid_price = Decimal(str(mid_price))

        # Compute width: spacing * levels (full grid width on one side)
        # For symmetric grid, total width = 2 * spacing * levels
        # But width_bps is defined as spacing * (levels_up + levels_down) / 2
        # which equals spacing * levels for symmetric
        width_bps = self.spacing_bps * self.levels

        # Uniform size schedule (same size at each level)
        size_schedule = [self.size_per_level] * self.levels

        return GridPlan(
            mode=GridMode.BILATERAL,
            center_price=mid_price,
            spacing_bps=self.spacing_bps,
            levels_up=self.levels,
            levels_down=self.levels,
            size_schedule=size_schedule,
            skew_bps=0.0,
            regime=MarketRegime.RANGE,
            width_bps=width_bps,
            reset_action=ResetAction.NONE,
            reason_codes=["REGIME_RANGE"],
        )

    def should_activate(self, features: dict[str, Any]) -> bool:  # noqa: ARG002
        """Check if this policy should be active.

        Static grid is always active (it's the fallback policy).

        Args:
            features: Dict of computed features (unused)

        Returns:
            Always True
        """
        return True
