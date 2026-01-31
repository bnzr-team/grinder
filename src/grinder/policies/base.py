"""Base grid policy interface.

See: docs/07_GRID_POLICY_LIBRARY.md, docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from grinder.core import GridMode, MarketRegime, ResetAction


@dataclass
class GridPlan:
    """Output of policy evaluation.

    Naming notes:
    - levels_up: levels above center (sell/short orders)
    - levels_down: levels below center (buy/long orders)
    - For symmetric grid: levels_up == levels_down

    See: docs/07_GRID_POLICY_LIBRARY.md section 1.1
    """

    mode: GridMode
    center_price: Decimal
    spacing_bps: float
    levels_up: int
    levels_down: int
    size_schedule: list[Decimal]
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
