"""Drawdown guard for equity protection.

This module provides a DrawdownGuard that:
- Tracks equity high-water mark (HWM)
- Computes current drawdown percentage
- Triggers when drawdown exceeds configurable threshold
- Integrates with KillSwitch for trading halt

Equity definition (ADR-013):
    equity = initial_capital + total_realized_pnl + total_unrealized_pnl

HWM is initialized to initial_capital on first update (or explicitly via reset).
Drawdown is computed as: (HWM - equity) / HWM

See: ADR-013 for drawdown guard design decisions
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - used at runtime
from typing import Any


@dataclass
class DrawdownCheckResult:
    """Result of a drawdown check.

    Attributes:
        equity: Current equity value
        high_water_mark: Current high-water mark
        drawdown_pct: Current drawdown as percentage (0.0 to 100.0)
        threshold_pct: Configured threshold
        triggered: Whether drawdown exceeded threshold
        details: Additional context
    """

    equity: Decimal
    high_water_mark: Decimal
    drawdown_pct: float
    threshold_pct: float
    triggered: bool
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "equity": str(self.equity),
            "high_water_mark": str(self.high_water_mark),
            "drawdown_pct": self.drawdown_pct,
            "threshold_pct": self.threshold_pct,
            "triggered": self.triggered,
            "details": self.details,
        }


class DrawdownGuard:
    """Guards against excessive drawdown from equity high-water mark.

    Tracks equity over time and computes drawdown from the peak (HWM).
    When drawdown exceeds the threshold, the guard triggers.

    Once triggered, the guard stays triggered (latched) until reset.
    This integrates with KillSwitch for trading halt.

    Usage:
        guard = DrawdownGuard(
            initial_capital=Decimal("10000"),
            max_drawdown_pct=5.0,  # 5% max drawdown
        )

        # Update with current equity after each snapshot
        result = guard.update(equity=Decimal("9600"))

        if result.triggered:
            # Drawdown exceeded 5% - trip kill switch
            kill_switch.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts)
    """

    def __init__(
        self,
        initial_capital: Decimal,
        max_drawdown_pct: float = 5.0,
    ) -> None:
        """Initialize drawdown guard.

        Args:
            initial_capital: Starting capital (used for initial HWM)
            max_drawdown_pct: Maximum allowed drawdown percentage (default 5%)
        """
        if initial_capital <= 0:
            msg = "initial_capital must be positive"
            raise ValueError(msg)
        if max_drawdown_pct <= 0 or max_drawdown_pct > 100:
            msg = "max_drawdown_pct must be between 0 and 100 (exclusive)"
            raise ValueError(msg)

        self._initial_capital = initial_capital
        self._max_drawdown_pct = max_drawdown_pct
        self._high_water_mark = initial_capital
        self._triggered = False
        self._trigger_equity: Decimal | None = None
        self._trigger_drawdown_pct: float | None = None

    @property
    def high_water_mark(self) -> Decimal:
        """Current high-water mark."""
        return self._high_water_mark

    @property
    def max_drawdown_pct(self) -> float:
        """Configured maximum drawdown threshold."""
        return self._max_drawdown_pct

    @property
    def is_triggered(self) -> bool:
        """Whether the drawdown guard has been triggered."""
        return self._triggered

    def update(self, equity: Decimal) -> DrawdownCheckResult:
        """Update with current equity and check drawdown.

        Updates HWM if equity exceeds current HWM.
        Computes drawdown and triggers if threshold exceeded.

        Once triggered, stays triggered (latched).

        Args:
            equity: Current equity value

        Returns:
            DrawdownCheckResult with current state and trigger status
        """
        # Update HWM if equity exceeds it
        self._high_water_mark = max(self._high_water_mark, equity)

        # Compute drawdown percentage: (HWM - equity) / HWM * 100
        if self._high_water_mark > 0:
            drawdown_pct = float((self._high_water_mark - equity) / self._high_water_mark * 100)
        else:
            drawdown_pct = 0.0

        # Clamp to non-negative (equity above HWM means 0% drawdown)
        drawdown_pct = max(0.0, drawdown_pct)

        # Check trigger condition (only if not already triggered)
        triggered_now = False
        if not self._triggered and drawdown_pct >= self._max_drawdown_pct:
            self._triggered = True
            self._trigger_equity = equity
            self._trigger_drawdown_pct = drawdown_pct
            triggered_now = True

        details: dict[str, Any] = {}
        if triggered_now:
            details["trigger_equity"] = str(equity)
            details["trigger_drawdown_pct"] = drawdown_pct
        if self._triggered and not triggered_now:
            # Already triggered previously
            details["previously_triggered"] = True
            if self._trigger_equity is not None:
                details["trigger_equity"] = str(self._trigger_equity)
            if self._trigger_drawdown_pct is not None:
                details["trigger_drawdown_pct"] = self._trigger_drawdown_pct

        return DrawdownCheckResult(
            equity=equity,
            high_water_mark=self._high_water_mark,
            drawdown_pct=drawdown_pct,
            threshold_pct=self._max_drawdown_pct,
            triggered=self._triggered,
            details=details if details else None,
        )

    def reset(self, initial_capital: Decimal | None = None) -> None:
        """Reset guard state for a new run.

        Args:
            initial_capital: Optional new initial capital (uses original if None)
        """
        if initial_capital is not None:
            if initial_capital <= 0:
                msg = "initial_capital must be positive"
                raise ValueError(msg)
            self._initial_capital = initial_capital
            self._high_water_mark = initial_capital
        else:
            self._high_water_mark = self._initial_capital

        self._triggered = False
        self._trigger_equity = None
        self._trigger_drawdown_pct = None
