"""Kill-switch for emergency trading halt.

This module provides a simple kill-switch latch that:
- Once triggered, stays triggered until explicit reset
- Is idempotent: triggering twice is a no-op
- Stores the reason and timestamp of the trigger
- Does NOT auto-reset within a run

See: ADR-013 for kill-switch design decisions
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class KillSwitchReason(Enum):
    """Reason for kill-switch activation.

    These values are stable and used in logs/metrics.
    """

    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    MANUAL = "MANUAL"
    ERROR = "ERROR"


@dataclass
class KillSwitchState:
    """Current state of the kill-switch.

    Attributes:
        triggered: Whether the kill-switch is active
        reason: Why it was triggered (None if not triggered)
        triggered_at_ts: Timestamp when triggered (None if not triggered)
        details: Additional context about the trigger
    """

    triggered: bool = False
    reason: KillSwitchReason | None = None
    triggered_at_ts: int | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "triggered": self.triggered,
            "reason": self.reason.value if self.reason else None,
            "triggered_at_ts": self.triggered_at_ts,
            "details": self.details,
        }


class KillSwitch:
    """Emergency kill-switch for trading halt.

    A simple latch that once triggered, stays triggered until explicit reset.
    Triggering multiple times is idempotent (no-op after first trigger).

    Usage:
        kill_switch = KillSwitch()

        # Check before trading
        if kill_switch.is_triggered:
            return  # Don't trade

        # Trip on error/risk breach
        kill_switch.trip(KillSwitchReason.DRAWDOWN_LIMIT, ts=1234567890)

        # After trip, all checks return True
        assert kill_switch.is_triggered

        # Reset only on new run (explicit call required)
        kill_switch.reset()
    """

    def __init__(self) -> None:
        """Initialize kill-switch in non-triggered state."""
        self._state = KillSwitchState()

    @property
    def is_triggered(self) -> bool:
        """Check if kill-switch is currently active."""
        return self._state.triggered

    @property
    def state(self) -> KillSwitchState:
        """Get current kill-switch state."""
        return self._state

    def trip(
        self,
        reason: KillSwitchReason,
        ts: int,
        details: dict[str, Any] | None = None,
    ) -> KillSwitchState:
        """Trip the kill-switch.

        Idempotent: if already triggered, returns current state without changes.

        Args:
            reason: Why the kill-switch is being triggered
            ts: Timestamp of the trigger event
            details: Optional additional context

        Returns:
            Current state after operation
        """
        if self._state.triggered:
            # Already triggered - no-op (idempotent)
            return self._state

        self._state = KillSwitchState(
            triggered=True,
            reason=reason,
            triggered_at_ts=ts,
            details=details,
        )
        return self._state

    def reset(self) -> None:
        """Reset kill-switch to non-triggered state.

        Should only be called when starting a fresh engine run.
        """
        self._state = KillSwitchState()
