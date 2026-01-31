"""Paper trading module for dry-run execution with gating.

Provides:
- PaperEngine: Paper trading loop with rate limiting and risk controls
- PaperOutput: Per-tick output with gating decisions
- PaperResult: Full run result with metrics and digest
"""

from grinder.paper.engine import PaperEngine, PaperOutput, PaperResult

__all__ = [
    "PaperEngine",
    "PaperOutput",
    "PaperResult",
]
