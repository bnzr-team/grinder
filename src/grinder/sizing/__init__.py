"""Position sizing module for risk-aware order quantities.

This module provides automatic position sizing based on risk budgets,
ensuring worst-case losses stay within defined drawdown limits.

Key components:
- AutoSizer: Computes size schedules from risk parameters
- SizeSchedule: Output with quantities and risk metrics
- GridShape: Input describing grid structure
"""

from grinder.sizing.auto_sizer import (
    AutoSizer,
    AutoSizerConfig,
    GridShape,
    SizeSchedule,
    SizingError,
)

__all__ = [
    "AutoSizer",
    "AutoSizerConfig",
    "GridShape",
    "SizeSchedule",
    "SizingError",
]
