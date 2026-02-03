"""Paper trading module for dry-run execution with gating.

Provides:
- PaperEngine: Paper trading loop with rate limiting and risk controls
- PaperOutput: Per-tick output with gating decisions and fills
- PaperResult: Full run result with metrics, positions, PnL, and digest
- Fill: Simulated fill event
- Ledger: Position and PnL tracking
- PositionState: Per-symbol position state
- PnLSnapshot: Point-in-time PnL snapshot
- CycleEngine: Fill → TP + replenishment for grid cycles
- CycleIntent: Intent to place TP or replenishment order
- CycleResult: Result from CycleEngine processing
- SCHEMA_VERSION: Current output schema version
"""

from grinder.paper.cycle_engine import CycleEngine, CycleIntent, CycleResult
from grinder.paper.engine import (
    SCHEMA_VERSION,
    PaperEngine,
    PaperOutput,
    PaperResult,
)
from grinder.paper.fills import Fill, simulate_fills
from grinder.paper.ledger import Ledger, PnLSnapshot, PositionState

__all__ = [
    "SCHEMA_VERSION",
    "CycleEngine",
    "CycleIntent",
    "CycleResult",
    "Fill",
    "Ledger",
    "PaperEngine",
    "PaperOutput",
    "PaperResult",
    "PnLSnapshot",
    "PositionState",
    "simulate_fills",
]
