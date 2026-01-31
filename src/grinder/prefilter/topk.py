"""Top-K symbol selection for prefilter v0.

This module implements deterministic Top-K symbol selection based on
volatility proxy scoring. Symbols are ranked by the sum of absolute
mid-price returns over a configurable window.

See: docs/04_PREFILTER_SPEC.md, ADR-010
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class SymbolScore:
    """Score for a single symbol.

    Attributes:
        symbol: Trading symbol
        score_bps: Volatility score as integer basis points (quantized for determinism)
        event_count: Number of events used in scoring
    """

    symbol: str
    score_bps: int  # Integer bps for deterministic sorting (no float!)
    event_count: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "symbol": self.symbol,
            "score_bps": self.score_bps,
            "event_count": self.event_count,
        }


@dataclass
class TopKResult:
    """Result of Top-K selection.

    Attributes:
        selected: Ordered list of selected symbols (highest score first)
        scores: All symbol scores (for observability)
        k: The K value used
    """

    selected: list[str]
    scores: list[SymbolScore]
    k: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "selected": self.selected,
            "scores": [s.to_dict() for s in self.scores],
            "k": self.k,
        }


@dataclass
class TopKSelector:
    """Deterministic Top-K symbol selector based on volatility proxy.

    Scoring method: Sum of absolute mid-price returns in basis points
    over the last N events per symbol:

        score = Σ |((mid_t - mid_{t-1}) / mid_{t-1}) * 10000|

    Tie-breakers (deterministic):
    1. Higher score first
    2. Lexicographic symbol ascending (stable)

    Attributes:
        k: Number of symbols to select (default 3)
        window_size: Number of events per symbol for scoring (default 10)
    """

    k: int = 3
    window_size: int = 10

    # Internal state: per-symbol price history
    # Each deque holds (ts, mid_price) tuples
    _price_history: dict[str, deque[tuple[int, Decimal]]] = field(default_factory=dict, repr=False)

    def _get_history(self, symbol: str) -> deque[tuple[int, Decimal]]:
        """Get or create price history for symbol."""
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=self.window_size)
        return self._price_history[symbol]

    def record_price(self, ts: int, symbol: str, mid_price: Decimal) -> None:
        """Record a price observation for scoring.

        Args:
            ts: Timestamp in milliseconds
            symbol: Trading symbol
            mid_price: Mid price at this timestamp
        """
        history = self._get_history(symbol)
        history.append((ts, mid_price))

    def _compute_score(self, symbol: str) -> SymbolScore:
        """Compute volatility score for a symbol.

        Returns sum of absolute returns in integer basis points (quantized).
        Using int ensures deterministic sorting across all platforms.
        """
        history = self._get_history(symbol)

        if len(history) < 2:
            return SymbolScore(symbol=symbol, score_bps=0, event_count=len(history))

        total_abs_return = Decimal("0")
        prices = [p for _, p in history]

        for i in range(1, len(prices)):
            prev_price = prices[i - 1]
            curr_price = prices[i]
            if prev_price > 0:
                abs_return = abs((curr_price - prev_price) / prev_price)
                total_abs_return += abs_return

        # Convert to integer basis points (quantized for determinism)
        # Multiply by 10000, then truncate to int
        score_bps = int((total_abs_return * 10000).quantize(Decimal("1")))

        return SymbolScore(
            symbol=symbol,
            score_bps=score_bps,
            event_count=len(history),
        )

    def select(self) -> TopKResult:
        """Select Top-K symbols based on current scores.

        Returns:
            TopKResult with selected symbols and all scores
        """
        # Compute scores for all symbols with history
        all_scores = [self._compute_score(symbol) for symbol in self._price_history]

        # Sort by score_bps descending, then symbol ascending (deterministic tie-break)
        # Using tuple: (-score_bps, symbol) for proper ordering
        # score_bps is int, so sorting is fully deterministic
        all_scores.sort(key=lambda s: (-s.score_bps, s.symbol))

        # Select top K
        selected = [s.symbol for s in all_scores[: self.k]]

        return TopKResult(
            selected=selected,
            scores=all_scores,
            k=self.k,
        )

    def get_all_symbols(self) -> list[str]:
        """Get all symbols that have been recorded."""
        return list(self._price_history.keys())

    def reset(self) -> None:
        """Reset all state."""
        self._price_history.clear()
