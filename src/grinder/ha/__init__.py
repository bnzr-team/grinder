"""HA (High Availability) module for single-host redundancy.

This module provides single-active safety through Redis lease-lock coordination.

Architecture:
- Multiple instances run on the same host
- Only one instance is "active" (holds the lock)
- Others are "standby" (ready to take over)
- Uses TTL-based lease lock for coordination

Components:
- HARole: Enum for active/standby roles
- HAState: Global state container for current role
- LeaderElector: Redis-based lease lock manager
"""

from grinder.ha.leader import LeaderElector, LeaderElectorConfig
from grinder.ha.role import HARole, HAState, get_ha_state, set_ha_state

__all__ = [
    "HARole",
    "HAState",
    "LeaderElector",
    "LeaderElectorConfig",
    "get_ha_state",
    "set_ha_state",
]
