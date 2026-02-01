"""Live runtime contract: stable /healthz and /metrics responses.

This module provides pure functions for building endpoint responses,
making them testable without network operations.

Contract (enforced by tests/unit/test_live_contracts.py):

GET /healthz:
    - Status: 200
    - Content-Type: application/json
    - Body: {"status": "ok", "uptime_s": <float>}

GET /metrics:
    - Status: 200
    - Content-Type: text/plain; charset=utf-8
    - Body: Prometheus text format with required metrics:
        - grinder_up (gauge)
        - grinder_uptime_seconds (gauge)
        - grinder_gating_allowed_total (counter)
        - grinder_gating_blocked_total (counter)
"""

from __future__ import annotations

import json
import time

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


def build_metrics_body() -> str:
    """Build /metrics response body.

    Returns:
        Prometheus text format metrics.
    """
    return build_metrics_output()


# Required metric patterns for contract validation
REQUIRED_HEALTHZ_KEYS = ["status", "uptime_s"]

REQUIRED_METRICS_PATTERNS = [
    "# HELP grinder_up",
    "# TYPE grinder_up",
    "grinder_up 1",
    "# HELP grinder_uptime_seconds",
    "# TYPE grinder_uptime_seconds",
    "grinder_uptime_seconds",
    "# HELP grinder_gating_allowed_total",
    "# TYPE grinder_gating_allowed_total",
    "# HELP grinder_gating_blocked_total",
    "# TYPE grinder_gating_blocked_total",
]
