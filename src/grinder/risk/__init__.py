"""Risk management components.

This module provides risk controls for trading:
- DrawdownGuard: tracks equity high-water mark and triggers on excessive drawdown
- DrawdownGuardV1: portfolio + per-symbol DD guard with intent-based blocking (ASM-P2-03)
- KillSwitch: emergency halt latch for trading
"""

from grinder.risk.drawdown import DrawdownCheckResult, DrawdownGuard
from grinder.risk.drawdown_guard_v1 import (
    AllowDecision,
    AllowReason,
    DrawdownGuardV1,
    DrawdownGuardV1Config,
    GuardError,
    GuardSnapshot,
    GuardState,
    OrderIntent,
)
from grinder.risk.kill_switch import KillSwitch, KillSwitchReason, KillSwitchState

__all__ = [
    "AllowDecision",
    "AllowReason",
    "DrawdownCheckResult",
    "DrawdownGuard",
    "DrawdownGuardV1",
    "DrawdownGuardV1Config",
    "GuardError",
    "GuardSnapshot",
    "GuardState",
    "KillSwitch",
    "KillSwitchReason",
    "KillSwitchState",
    "OrderIntent",
]
