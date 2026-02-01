"""Adaptive Controller v0 with rule-based parameter adjustment.

This module implements a deterministic controller that adjusts policy
parameters based on recent market conditions using window-based metrics.

Decision logic (priority order):
1. spread_bps_max > 50 → PAUSE (WIDE_SPREAD)
2. vol_bps > 300 → WIDEN (HIGH_VOL)
3. vol_bps < 50 → TIGHTEN (LOW_VOL)
4. else → BASE (NORMAL)

All metrics use integer basis points for determinism.

See: docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md, ADR-011
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from grinder.controller.types import ControllerDecision, ControllerMode, ControllerReason


@dataclass
class AdaptiveController:
    """Rule-based adaptive controller for grid parameter adjustment.

    Tracks per-symbol price and spread history over a sliding window.
    Computes volatility (sum of abs mid returns) and max spread in bps.
    Returns deterministic decisions based on threshold rules.

    Attributes:
        window_size: Number of events per symbol for metrics (default 10)
        spread_pause_bps: Spread threshold for PAUSE mode (default 50)
        vol_widen_bps: Volatility threshold for WIDEN mode (default 300)
        vol_tighten_bps: Volatility threshold for TIGHTEN mode (default 50)
        widen_multiplier: Spacing multiplier for WIDEN mode (default 1.5)
        tighten_multiplier: Spacing multiplier for TIGHTEN mode (default 0.8)
    """

    window_size: int = 10
    spread_pause_bps: int = 50
    vol_widen_bps: int = 300
    vol_tighten_bps: int = 50
    widen_multiplier: float = 1.5
    tighten_multiplier: float = 0.8

    # Internal state: per-symbol history
    # Each deque holds (ts, mid_price, spread_bps) tuples
    _history: dict[str, deque[tuple[int, Decimal, int]]] = field(default_factory=dict, repr=False)

    def _get_history(self, symbol: str) -> deque[tuple[int, Decimal, int]]:
        """Get or create history for symbol."""
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self.window_size)
        return self._history[symbol]

    def record(self, ts: int, symbol: str, mid_price: Decimal, spread_bps: float) -> None:
        """Record a price and spread observation.

        Args:
            ts: Timestamp in milliseconds
            symbol: Trading symbol
            mid_price: Mid price at this timestamp
            spread_bps: Spread in basis points (float, converted to int)
        """
        history = self._get_history(symbol)
        # Convert spread to integer bps for determinism
        spread_bps_int = int(spread_bps)
        history.append((ts, mid_price, spread_bps_int))

    def _compute_metrics(self, symbol: str) -> tuple[int, int, int]:
        """Compute window metrics for a symbol.

        Returns:
            (vol_bps, spread_bps_max, window_size) - all integers
        """
        history = self._get_history(symbol)

        if len(history) < 2:
            return (0, 0, len(history))

        # Compute volatility: sum of absolute mid returns in bps
        total_abs_return = Decimal("0")
        prices = [p for _, p, _ in history]

        for i in range(1, len(prices)):
            prev_price = prices[i - 1]
            curr_price = prices[i]
            if prev_price > 0:
                abs_return = abs((curr_price - prev_price) / prev_price)
                total_abs_return += abs_return

        # Convert to integer basis points (quantized for determinism)
        vol_bps = int((total_abs_return * 10000).quantize(Decimal("1")))

        # Compute max spread in window
        spreads = [s for _, _, s in history]
        spread_bps_max = max(spreads)

        return (vol_bps, spread_bps_max, len(history))

    def decide(self, symbol: str) -> ControllerDecision:
        """Make controller decision based on current window metrics.

        Decision logic (priority order):
        1. spread_bps_max > spread_pause_bps → PAUSE
        2. vol_bps > vol_widen_bps → WIDEN
        3. vol_bps < vol_tighten_bps → TIGHTEN
        4. else → BASE

        Args:
            symbol: Trading symbol

        Returns:
            ControllerDecision with mode, reason, and multiplier
        """
        vol_bps, spread_bps_max, window_size = self._compute_metrics(symbol)

        # Priority 1: Wide spread → PAUSE
        if spread_bps_max > self.spread_pause_bps:
            return ControllerDecision(
                mode=ControllerMode.PAUSE,
                reason=ControllerReason.WIDE_SPREAD,
                spacing_multiplier=1.0,  # N/A for PAUSE
                vol_bps=vol_bps,
                spread_bps_max=spread_bps_max,
                window_size=window_size,
            )

        # Priority 2: High volatility → WIDEN
        if vol_bps > self.vol_widen_bps:
            return ControllerDecision(
                mode=ControllerMode.WIDEN,
                reason=ControllerReason.HIGH_VOL,
                spacing_multiplier=self.widen_multiplier,
                vol_bps=vol_bps,
                spread_bps_max=spread_bps_max,
                window_size=window_size,
            )

        # Priority 3: Low volatility → TIGHTEN
        if vol_bps < self.vol_tighten_bps:
            return ControllerDecision(
                mode=ControllerMode.TIGHTEN,
                reason=ControllerReason.LOW_VOL,
                spacing_multiplier=self.tighten_multiplier,
                vol_bps=vol_bps,
                spread_bps_max=spread_bps_max,
                window_size=window_size,
            )

        # Default: BASE mode
        return ControllerDecision(
            mode=ControllerMode.BASE,
            reason=ControllerReason.NORMAL,
            spacing_multiplier=1.0,
            vol_bps=vol_bps,
            spread_bps_max=spread_bps_max,
            window_size=window_size,
        )

    def get_all_symbols(self) -> list[str]:
        """Get all symbols that have been recorded."""
        return list(self._history.keys())

    def reset(self) -> None:
        """Reset all state."""
        self._history.clear()
