"""Base grid policy interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from grinder.core import GridMode


@dataclass
class GridPlan:
    """Output of policy evaluation."""

    mode: GridMode
    center_price: Decimal
    spacing_bps: float
    levels_up: int
    levels_down: int
    size_schedule: list[Decimal]
    skew_bps: float = 0.0
    reason_codes: list[str] | None = None


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
