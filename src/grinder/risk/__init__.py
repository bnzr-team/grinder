"""Risk management components.

This module provides risk controls for trading:
- DrawdownGuard: tracks equity high-water mark and triggers on excessive drawdown
- KillSwitch: emergency halt latch for trading
"""

from grinder.risk.drawdown import DrawdownCheckResult, DrawdownGuard
from grinder.risk.kill_switch import KillSwitch, KillSwitchReason, KillSwitchState

__all__ = [
    "DrawdownCheckResult",
    "DrawdownGuard",
    "KillSwitch",
    "KillSwitchReason",
    "KillSwitchState",
]
