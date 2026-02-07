"""Budget state lifecycle management (M4.2).

Provides utilities for budget state file operations:
- reset_budget_state(): Deletes budget state file for clean start
- check_budget_state_stale(): Warns if state file is older than threshold

Environment Variables:
    BUDGET_STATE_STALE_HOURS  Hours before state is considered stale (default: 24)

See ROADMAP.md M4.2 for design decisions.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Environment variable names
ENV_BUDGET_STATE_STALE_HOURS = "BUDGET_STATE_STALE_HOURS"

# Default stale threshold
DEFAULT_STALE_HOURS = 24


def reset_budget_state(path: str | None) -> bool:
    """Delete budget state file for a clean start.

    Args:
        path: Path to budget state file (None = no-op)

    Returns:
        True if file was deleted or didn't exist, False on error
    """
    if path is None:
        logger.debug("BUDGET_STATE_RESET_SKIPPED reason=no_path_configured")
        return True

    state_path = Path(path)

    if not state_path.exists():
        logger.info(f"BUDGET_STATE_RESET budget_state_reset=1 path={path} status=not_found")
        print(f"  budget_state_reset=1 path={path} (file not found)")
        return True

    try:
        state_path.unlink()
        logger.info(f"BUDGET_STATE_RESET budget_state_reset=1 path={path} status=deleted")
        print(f"  budget_state_reset=1 path={path} (deleted)")
        return True
    except OSError as e:
        logger.error(f"BUDGET_STATE_RESET_FAILED path={path} error={e}")
        print(f"  budget_state_reset=0 path={path} error={e}")
        return False


def check_budget_state_stale(
    path: str | None,
    stale_hours: int | None = None,
) -> bool:
    """Check if budget state file is stale and warn if so.

    Args:
        path: Path to budget state file (None = no-op, returns False)
        stale_hours: Hours before state is considered stale (default: 24)

    Returns:
        True if file is stale (and warning was logged), False otherwise
    """
    if path is None:
        return False

    if stale_hours is None:
        stale_hours_str = os.environ.get(ENV_BUDGET_STATE_STALE_HOURS, "").strip()
        try:
            stale_hours = int(stale_hours_str) if stale_hours_str else DEFAULT_STALE_HOURS
        except ValueError:
            logger.warning(
                f"Invalid {ENV_BUDGET_STATE_STALE_HOURS}={stale_hours_str}, "
                f"using default {DEFAULT_STALE_HOURS}"
            )
            stale_hours = DEFAULT_STALE_HOURS

    state_path = Path(path)

    if not state_path.exists():
        return False

    try:
        mtime = state_path.stat().st_mtime
        mtime_dt = datetime.fromtimestamp(mtime, tz=UTC)
        age_hours = (datetime.now(UTC) - mtime_dt).total_seconds() / 3600

        if age_hours > stale_hours:
            age_str = f"{age_hours:.1f}h"
            mtime_str = mtime_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.warning(
                f"BUDGET_STATE_STALE path={path} age={age_str} "
                f"threshold={stale_hours}h mtime={mtime_str}"
            )
            print(f"  WARNING: Budget state is stale ({age_str} old, threshold={stale_hours}h)")
            print(f"           Last modified: {mtime_str}")
            print(f"           Path: {path}")
            print("           Consider using --reset-budget-state for a clean start.")
            return True

        return False

    except OSError as e:
        logger.warning(f"BUDGET_STATE_STALE_CHECK_FAILED path={path} error={e}")
        return False
