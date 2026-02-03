"""Base grid policy interface.

See: docs/07_GRID_POLICY_LIBRARY.md, docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from grinder.core import GridMode, MarketRegime, ResetAction


def notional_to_qty(
    notional: Decimal,
    price: Decimal,
    precision: int = 8,
) -> Decimal:
    """Convert notional (quote currency) to quantity (base asset).

    Formula: qty = notional / price

    Example:
        $500 notional at $50,000/BTC = 0.01 BTC

    Args:
        notional: Amount in quote currency (e.g., USD)
        price: Current price in quote/base (e.g., USD/BTC)
        precision: Decimal places for quantity rounding (default 8)

    Returns:
        Quantity in base asset, rounded down to precision

    Raises:
        ValueError: If price is zero or negative
        ValueError: If notional is negative

    See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.12.4 (ADR-018)
    """
    if price <= 0:
        raise ValueError("price must be positive")
    if notional < 0:
        raise ValueError("notional must be non-negative")

    qty = notional / price
    quantize_str = "0." + "0" * precision
    return qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)


@dataclass
class GridPlan:
    """Output of policy evaluation.

    Naming notes:
    - levels_up: levels above center (sell/short orders)
    - levels_down: levels below center (buy/long orders)
    - For symmetric grid: levels_up == levels_down

    Units (SSOT - see docs/17_ADAPTIVE_SMART_GRID_V1.md §17.12.4):
    - size_schedule: ALWAYS base asset quantity (e.g., BTC, ETH), NOT notional (USD)
    - center_price: quote currency per base unit (e.g., USD/BTC)

    See: docs/07_GRID_POLICY_LIBRARY.md section 1.1
    """

    mode: GridMode
    center_price: Decimal
    spacing_bps: float
    levels_up: int
    levels_down: int
    size_schedule: list[Decimal]  # Base asset quantity per level (NOT notional/USD)
    skew_bps: float = 0.0
    # New fields for Adaptive Controller integration
    regime: MarketRegime = MarketRegime.RANGE
    width_bps: float = 0.0  # Computed: spacing_bps * (levels_up + levels_down) / 2
    reset_action: ResetAction = ResetAction.NONE
    reason_codes: list[str] = field(default_factory=list)


class GridPolicy(ABC):
    """Abstract base class for grid policies."""

    name: str = "BASE"

    @abstractmethod
    def evaluate(self, features: dict[str, Any]) -> GridPlan:
        """
        Evaluate features and return grid plan.

        Args:
            features: Dict of computed features for the symbol

        Returns:
            GridPlan with mode, spacing, levels, sizes
        """
        ...

    @abstractmethod
    def should_activate(self, features: dict[str, Any]) -> bool:
        """
        Check if this policy should be active.

        Args:
            features: Dict of computed features

        Returns:
            True if policy conditions are met
        """
        ...
