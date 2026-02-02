"""HA role definitions and state management.

This module defines the possible HA roles and provides
thread-safe state management for the current role.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock


class HARole(Enum):
    """High Availability role for a grinder instance.

    ACTIVE: Instance holds the lease lock and is processing/trading
    STANDBY: Instance is healthy but waiting to acquire lock
    UNKNOWN: Initial state before first lock attempt
    """

    ACTIVE = "active"
    STANDBY = "standby"
    UNKNOWN = "unknown"


@dataclass
class HAState:
    """Container for HA state.

    Attributes:
        role: Current HA role
        instance_id: Unique identifier for this instance
        lock_holder: ID of current lock holder (if known)
        last_lock_attempt_ms: Timestamp of last lock attempt
        lock_failures: Count of consecutive lock failures
    """

    role: HARole = HARole.UNKNOWN
    instance_id: str = ""
    lock_holder: str | None = None
    last_lock_attempt_ms: int = 0
    lock_failures: int = 0


# Module-level state with thread-safe access
_state_lock = Lock()
_ha_state = HAState()


def get_ha_state() -> HAState:
    """Get current HA state (thread-safe copy)."""
    with _state_lock:
        return HAState(
            role=_ha_state.role,
            instance_id=_ha_state.instance_id,
            lock_holder=_ha_state.lock_holder,
            last_lock_attempt_ms=_ha_state.last_lock_attempt_ms,
            lock_failures=_ha_state.lock_failures,
        )


def set_ha_state(
    *,
    role: HARole | None = None,
    instance_id: str | None = None,
    lock_holder: str | None = None,
    last_lock_attempt_ms: int | None = None,
    lock_failures: int | None = None,
) -> None:
    """Update HA state (thread-safe).

    Only provided fields are updated; others remain unchanged.
    """
    with _state_lock:
        if role is not None:
            _ha_state.role = role
        if instance_id is not None:
            _ha_state.instance_id = instance_id
        if lock_holder is not None:
            _ha_state.lock_holder = lock_holder
        if last_lock_attempt_ms is not None:
            _ha_state.last_lock_attempt_ms = last_lock_attempt_ms
        if lock_failures is not None:
            _ha_state.lock_failures = lock_failures


def reset_ha_state() -> None:
    """Reset HA state to defaults (for testing)."""
    with _state_lock:
        _ha_state.role = HARole.UNKNOWN
        _ha_state.instance_id = ""
        _ha_state.lock_holder = None
        _ha_state.last_lock_attempt_ms = 0
        _ha_state.lock_failures = 0
