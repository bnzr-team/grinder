"""P0-2: Recent-places correlation helper for AccountSync debug."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections import deque


@dataclass(frozen=True)
class CorrelationResult:
    """Result of correlating recent PLACEs against AccountSync open_orders."""

    total: int
    found: int
    missing: int
    missing_details: list[str]


def correlate_recent_places(
    recent: deque[tuple[str, int, str]],
    open_ids: set[str],
    now_ms: int,
) -> CorrelationResult:
    """Compare recent PLACEs against AccountSync open_orders.

    Args:
        recent: deque of (client_order_id, placed_wall_ts_ms, symbol).
        open_ids: Set of order_ids from AccountSync snapshot.
        now_ms: Current wall-clock ms (for age calculation).

    Returns:
        CorrelationResult with counts and missing details.
    """
    found = 0
    missing_details: list[str] = []
    for cid, placed_ts, sym in recent:
        if cid in open_ids:
            found += 1
        else:
            age_ms = now_ms - placed_ts
            missing_details.append(f"{cid}(age={age_ms}ms sym={sym})")
    return CorrelationResult(
        total=len(recent),
        found=found,
        missing=len(recent) - found,
        missing_details=missing_details,
    )
