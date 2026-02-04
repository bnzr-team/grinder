"""Position sizing module for risk-aware order quantities.

This module provides automatic position sizing based on risk budgets,
ensuring worst-case losses stay within defined drawdown limits.

Key components:
- AutoSizer: Computes size schedules from risk parameters
- SizeSchedule: Output with quantities and risk metrics
- GridShape: Input describing grid structure
- DdAllocator: Distributes portfolio DD budget across symbols
"""

from grinder.sizing.auto_sizer import (
    AutoSizer,
    AutoSizerConfig,
    GridShape,
    SizeSchedule,
    SizingError,
)
from grinder.sizing.dd_allocator import (
    AllocationError,
    AllocationResult,
    DdAllocator,
    DdAllocatorConfig,
    RiskTier,
    SymbolCandidate,
)

__all__ = [
    "AllocationError",
    "AllocationResult",
    "AutoSizer",
    "AutoSizerConfig",
    "DdAllocator",
    "DdAllocatorConfig",
    "GridShape",
    "RiskTier",
    "SizeSchedule",
    "SizingError",
    "SymbolCandidate",
]
