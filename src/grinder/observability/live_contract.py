"""Live runtime contract: stable /healthz, /readyz, and /metrics responses.

This module provides pure functions for building endpoint responses,
making them testable without network operations.

Contract (enforced by tests/unit/test_live_contracts.py):

GET /healthz:
    - Status: 200 (always, if process is alive)
    - Content-Type: application/json
    - Body: {"status": "ok", "uptime_s": <float>}

GET /readyz:
    - Status: 200 if ACTIVE (ready to handle traffic/trading)
    - Status: 503 if STANDBY or UNKNOWN (not ready)
    - Content-Type: application/json
    - Body: {"ready": true/false, "role": "active"|"standby"|"unknown"}

GET /metrics:
    - Status: 200
    - Content-Type: text/plain; charset=utf-8
    - Body: Prometheus text format with required metrics:
        - grinder_up (gauge)
        - grinder_uptime_seconds (gauge)
        - grinder_gating_allowed_total (counter)
        - grinder_gating_blocked_total (counter)
        - grinder_kill_switch_triggered (gauge)
        - grinder_kill_switch_trips_total (counter)
        - grinder_drawdown_pct (gauge)
        - grinder_high_water_mark (gauge)
        - grinder_ha_role (gauge with role label)
        - grinder_connector_retries_total (counter, H2)
        - grinder_idempotency_hits_total (counter, H3)
        - grinder_idempotency_conflicts_total (counter, H3)
        - grinder_idempotency_misses_total (counter, H3)
        - grinder_circuit_state (gauge, H4)
        - grinder_circuit_rejected_total (counter, H4)
        - grinder_circuit_trips_total (counter, H4)
"""

from __future__ import annotations

import json
import time

from grinder.ha.role import HARole, get_ha_state
from grinder.observability.metrics_builder import build_metrics_output

# Module-level state container (avoids global statement)
# Uses list as mutable container: [start_time_or_None]
_state: list[float | None] = [None]


def get_start_time() -> float:
    """Get or initialize the start time."""
    if _state[0] is None:
        _state[0] = time.time()
    result = _state[0]
    assert result is not None  # Always initialized above
    return result


def reset_start_time() -> None:
    """Reset start time (for testing)."""
    _state[0] = None


def set_start_time(t: float) -> None:
    """Set start time explicitly (for testing)."""
    _state[0] = t


def build_healthz_body() -> str:
    """Build /healthz response body.

    Returns:
        JSON string with status and uptime.
    """
    start = get_start_time()
    uptime = time.time() - start
    return json.dumps(
        {
            "status": "ok",
            "uptime_s": round(uptime, 2),
        }
    )


def build_readyz_body() -> tuple[str, bool]:
    """Build /readyz response body.

    Returns:
        Tuple of (JSON string body, is_ready boolean).
        is_ready is True only if HA role is ACTIVE.
    """
    state = get_ha_state()
    is_ready = state.role == HARole.ACTIVE
    return (
        json.dumps(
            {
                "ready": is_ready,
                "role": state.role.value,
            }
        ),
        is_ready,
    )


def build_metrics_body() -> str:
    """Build /metrics response body.

    Returns:
        Prometheus text format metrics.
    """
    return build_metrics_output()


# Required metric patterns for contract validation
REQUIRED_HEALTHZ_KEYS = ["status", "uptime_s"]
REQUIRED_READYZ_KEYS = ["ready", "role"]

REQUIRED_METRICS_PATTERNS = [
    # System metrics
    "# HELP grinder_up",
    "# TYPE grinder_up",
    "grinder_up 1",
    "# HELP grinder_uptime_seconds",
    "# TYPE grinder_uptime_seconds",
    "grinder_uptime_seconds",
    # Gating metrics
    "# HELP grinder_gating_allowed_total",
    "# TYPE grinder_gating_allowed_total",
    "# HELP grinder_gating_blocked_total",
    "# TYPE grinder_gating_blocked_total",
    # Risk metrics
    "# HELP grinder_kill_switch_triggered",
    "# TYPE grinder_kill_switch_triggered",
    "grinder_kill_switch_triggered",
    "# HELP grinder_kill_switch_trips_total",
    "# TYPE grinder_kill_switch_trips_total",
    "# HELP grinder_drawdown_pct",
    "# TYPE grinder_drawdown_pct",
    "grinder_drawdown_pct",
    "# HELP grinder_high_water_mark",
    "# TYPE grinder_high_water_mark",
    "grinder_high_water_mark",
    # HA metrics
    "# HELP grinder_ha_role",
    "# TYPE grinder_ha_role",
    "grinder_ha_role",
    # Connector metrics (H2/H3/H4)
    "# HELP grinder_connector_retries_total",
    "# TYPE grinder_connector_retries_total",
    "# HELP grinder_idempotency_hits_total",
    "# TYPE grinder_idempotency_hits_total",
    "# HELP grinder_idempotency_conflicts_total",
    "# TYPE grinder_idempotency_conflicts_total",
    "# HELP grinder_idempotency_misses_total",
    "# TYPE grinder_idempotency_misses_total",
    "# HELP grinder_circuit_state",
    "# TYPE grinder_circuit_state",
    "# HELP grinder_circuit_rejected_total",
    "# TYPE grinder_circuit_rejected_total",
    "# HELP grinder_circuit_trips_total",
    "# TYPE grinder_circuit_trips_total",
]
