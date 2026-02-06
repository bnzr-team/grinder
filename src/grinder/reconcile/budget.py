"""Remediation budget tracking and persistence (LC-18).

See ADR-046 for design decisions.

This module provides:
- BudgetState: Current budget usage (calls, notional)
- BudgetTracker: Manages budget limits and persistence
- Daily reset: Budgets reset at midnight UTC

Budget enforcement:
- Per-run limits: Checked within a single reconcile run
- Per-day limits: Persisted across runs, reset at midnight UTC

Persistence format (JSON):
{
    "date": "2024-01-15",
    "calls_today": 5,
    "notional_today": "1234.56",
    "last_updated_ts_ms": 1705320000000
}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BudgetState:
    """Current budget usage state.

    Attributes:
        date: Date string (YYYY-MM-DD) for daily tracking
        calls_today: Number of remediation calls today
        notional_today: Total notional USDT remediated today
        calls_this_run: Number of calls in current run
        notional_this_run: Total notional in current run
        last_updated_ts_ms: Last update timestamp (ms)
    """

    date: str = ""
    calls_today: int = 0
    notional_today: Decimal = field(default_factory=lambda: Decimal("0"))
    calls_this_run: int = 0
    notional_this_run: Decimal = field(default_factory=lambda: Decimal("0"))
    last_updated_ts_ms: int = 0

    def to_dict(self) -> dict[str, str | int]:
        """Serialize to dict for JSON persistence."""
        return {
            "date": self.date,
            "calls_today": self.calls_today,
            "notional_today": str(self.notional_today),
            "last_updated_ts_ms": self.last_updated_ts_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetState:
        """Deserialize from dict."""
        return cls(
            date=data.get("date", ""),
            calls_today=data.get("calls_today", 0),
            notional_today=Decimal(data.get("notional_today", "0")),
            calls_this_run=0,  # Never persisted
            notional_this_run=Decimal("0"),  # Never persisted
            last_updated_ts_ms=data.get("last_updated_ts_ms", 0),
        )


@dataclass
class BudgetTracker:
    """Manages remediation budget limits and persistence.

    Thread-safety: No (use separate instances per thread)

    Attributes:
        max_calls_per_day: Maximum remediation calls per calendar day
        max_notional_per_day: Maximum notional USDT per calendar day
        max_calls_per_run: Maximum calls per reconcile run
        max_notional_per_run: Maximum notional per reconcile run
        state_path: Optional path for persistent state (None = in-memory)
    """

    max_calls_per_day: int = 100
    max_notional_per_day: Decimal = field(default_factory=lambda: Decimal("5000"))
    max_calls_per_run: int = 10
    max_notional_per_run: Decimal = field(default_factory=lambda: Decimal("1000"))
    state_path: str | None = None

    # Internal state
    _state: BudgetState = field(default_factory=BudgetState)

    def __post_init__(self) -> None:
        """Load state from disk if path is configured."""
        if self.state_path:
            self._load_state()
        self._check_date_reset()

    def _get_today_str(self) -> str:
        """Get today's date string in UTC."""
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _check_date_reset(self) -> None:
        """Reset daily counters if date has changed."""
        today = self._get_today_str()
        if self._state.date != today:
            logger.info(
                "BUDGET_DAILY_RESET",
                extra={
                    "old_date": self._state.date,
                    "new_date": today,
                    "old_calls": self._state.calls_today,
                    "old_notional": str(self._state.notional_today),
                },
            )
            self._state.date = today
            self._state.calls_today = 0
            self._state.notional_today = Decimal("0")
            self._save_state()

    def _load_state(self) -> None:
        """Load state from disk."""
        if not self.state_path:
            return

        path = Path(self.state_path)
        if not path.exists():
            logger.info("BUDGET_STATE_NOT_FOUND", extra={"path": self.state_path})
            return

        try:
            data = json.loads(path.read_text())
            self._state = BudgetState.from_dict(data)
            logger.info(
                "BUDGET_STATE_LOADED",
                extra={
                    "path": self.state_path,
                    "date": self._state.date,
                    "calls_today": self._state.calls_today,
                    "notional_today": str(self._state.notional_today),
                },
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "BUDGET_STATE_LOAD_ERROR",
                extra={"path": self.state_path, "error": str(e)},
            )
            # Start fresh on error
            self._state = BudgetState()

    def _save_state(self) -> None:
        """Save state to disk."""
        if not self.state_path:
            return

        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state.last_updated_ts_ms = int(datetime.now(UTC).timestamp() * 1000)
            path.write_text(json.dumps(self._state.to_dict(), indent=2))
        except OSError as e:
            logger.error(
                "BUDGET_STATE_SAVE_ERROR",
                extra={"path": self.state_path, "error": str(e)},
            )

    def reset_run_counters(self) -> None:
        """Reset per-run counters. Call at start of each reconcile run."""
        self._check_date_reset()
        self._state.calls_this_run = 0
        self._state.notional_this_run = Decimal("0")

    def can_execute(self, notional_usdt: Decimal = Decimal("0")) -> tuple[bool, str | None]:
        """Check if execution is within budget limits.

        Args:
            notional_usdt: Notional value of the planned action

        Returns:
            (can_execute, block_reason): Tuple of result and optional reason string
        """
        self._check_date_reset()

        # Check per-run call limit
        if self._state.calls_this_run >= self.max_calls_per_run:
            return (False, "max_calls_per_run")

        # Check per-run notional limit
        if self._state.notional_this_run + notional_usdt > self.max_notional_per_run:
            return (False, "max_notional_per_run")

        # Check per-day call limit
        if self._state.calls_today >= self.max_calls_per_day:
            return (False, "max_calls_per_day")

        # Check per-day notional limit
        if self._state.notional_today + notional_usdt > self.max_notional_per_day:
            return (False, "max_notional_per_day")

        return (True, None)

    def record_execution(self, notional_usdt: Decimal = Decimal("0")) -> None:
        """Record a successful remediation execution.

        Args:
            notional_usdt: Notional value of the executed action
        """
        self._state.calls_this_run += 1
        self._state.notional_this_run += notional_usdt
        self._state.calls_today += 1
        self._state.notional_today += notional_usdt
        self._save_state()

        logger.info(
            "BUDGET_RECORDED",
            extra={
                "calls_this_run": self._state.calls_this_run,
                "notional_this_run": str(self._state.notional_this_run),
                "calls_today": self._state.calls_today,
                "notional_today": str(self._state.notional_today),
            },
        )

    def get_remaining(self) -> dict[str, int | Decimal]:
        """Get remaining budget for metrics/observability.

        Returns:
            Dict with remaining calls and notional for run and day
        """
        self._check_date_reset()
        return {
            "calls_remaining_run": max(0, self.max_calls_per_run - self._state.calls_this_run),
            "notional_remaining_run": max(
                Decimal("0"), self.max_notional_per_run - self._state.notional_this_run
            ),
            "calls_remaining_day": max(0, self.max_calls_per_day - self._state.calls_today),
            "notional_remaining_day": max(
                Decimal("0"), self.max_notional_per_day - self._state.notional_today
            ),
        }

    def get_used(self) -> dict[str, int | Decimal]:
        """Get used budget for metrics/observability.

        Returns:
            Dict with used calls and notional for run and day
        """
        return {
            "calls_used_run": self._state.calls_this_run,
            "notional_used_run": self._state.notional_this_run,
            "calls_used_day": self._state.calls_today,
            "notional_used_day": self._state.notional_today,
        }
