"""Consecutive loss guard v1 (Track C, PR-C3).

Tracks consecutive losing roundtrips and trips when the count reaches
a configurable threshold.  Pure logic — no I/O, no metrics emission.
The caller is responsible for evidence/metrics/FSM operator_override.

State machine
-------------
* ``outcome == "loss"`` → count++.
* ``outcome in {"win", "breakeven"}`` → count = 0 (reset).
* Unknown outcomes → no-op (ignored).
* ``count >= threshold`` → tripped.

Design constraints:
- Pure logic: no I/O, no logging, no imports beyond stdlib + grinder types.
- Deterministic: same sequence of update() calls → same state.
- Safe-by-default: disabled when ``enabled=False`` (env-gated by caller).

SSOT: this module.  ADR-070 in docs/DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --- Known outcomes --------------------------------------------------------

KNOWN_OUTCOMES: frozenset[str] = frozenset({"win", "loss", "breakeven"})


# --- Configuration ---------------------------------------------------------


class ConsecutiveLossAction(Enum):
    """Action to take when the guard trips.

    Values are stable strings used in evidence/logs.
    """

    PAUSE = "PAUSE"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class ConsecutiveLossConfig:
    """Configuration for ConsecutiveLossGuard.

    Attributes:
        enabled: Whether the guard is active.
        threshold: Number of consecutive losses to trigger trip (>=1).
        action: FSM action on trip (PAUSE or DEGRADED).
    """

    enabled: bool = False
    threshold: int = 5
    action: ConsecutiveLossAction = ConsecutiveLossAction.PAUSE

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ConsecutiveLossGuardError(f"threshold must be >= 1, got {self.threshold}")


# --- Exception -------------------------------------------------------------


class ConsecutiveLossGuardError(Exception):
    """Non-retryable error in ConsecutiveLossGuard."""


# --- State -----------------------------------------------------------------


@dataclass(frozen=True)
class ConsecutiveLossState:
    """Immutable snapshot of guard state.

    Attributes:
        count: Current consecutive loss count.
        tripped: Whether threshold was reached.
        last_row_id: row_id of the last processed outcome.
        last_ts_ms: Timestamp (ms) of the last processed outcome.
    """

    count: int = 0
    tripped: bool = False
    last_row_id: str | None = None
    last_ts_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for evidence/logging."""
        return {
            "count": self.count,
            "tripped": self.tripped,
            "last_row_id": self.last_row_id,
            "last_ts_ms": self.last_ts_ms,
        }


# --- Guard -----------------------------------------------------------------


@dataclass
class ConsecutiveLossGuard:
    """Consecutive loss limit guard v1.

    Pure logic.  No I/O, no metrics.  Caller is responsible for
    side effects (evidence, metrics, FSM operator_override).

    Usage::

        config = ConsecutiveLossConfig(enabled=True, threshold=3)
        guard = ConsecutiveLossGuard(config)

        tripped = guard.update(outcome="loss", row_id="r1", ts_ms=1000)
        # False (count=1)
        tripped = guard.update(outcome="loss", row_id="r2", ts_ms=2000)
        # False (count=2)
        tripped = guard.update(outcome="loss", row_id="r3", ts_ms=3000)
        # True (count=3, tripped!)

        guard.reset()
        # count=0, tripped=False
    """

    config: ConsecutiveLossConfig = field(
        default_factory=ConsecutiveLossConfig,
    )
    _state: ConsecutiveLossState = field(
        default_factory=ConsecutiveLossState,
        init=False,
        repr=False,
    )

    @property
    def state(self) -> ConsecutiveLossState:
        """Current guard state snapshot."""
        return self._state

    @property
    def count(self) -> int:
        """Current consecutive loss count."""
        return self._state.count

    @property
    def is_tripped(self) -> bool:
        """Whether the guard has tripped (count >= threshold)."""
        return self._state.tripped

    def update(
        self,
        outcome: str,
        *,
        row_id: str | None = None,
        ts_ms: int | None = None,
    ) -> bool:
        """Process a roundtrip outcome.

        Args:
            outcome: Roundtrip result ("win", "loss", "breakeven").
            row_id: Optional FillOutcomeRow.row_id for evidence.
            ts_ms: Optional timestamp (ms) for evidence.

        Returns:
            True if the guard tripped on THIS update (newly tripped).
            False otherwise (including already-tripped, reset, or no-op).
        """
        if not self.config.enabled:
            return False

        if outcome not in KNOWN_OUTCOMES:
            # Unknown outcome → no-op.
            return False

        was_tripped = self._state.tripped

        if outcome == "loss":
            new_count = self._state.count + 1
            new_tripped = new_count >= self.config.threshold
            self._state = ConsecutiveLossState(
                count=new_count,
                tripped=new_tripped,
                last_row_id=row_id,
                last_ts_ms=ts_ms,
            )
            # Return True only on the transition from not-tripped to tripped.
            return new_tripped and not was_tripped

        # Win or breakeven → reset.
        self._state = ConsecutiveLossState(
            count=0,
            tripped=False,
            last_row_id=row_id,
            last_ts_ms=ts_ms,
        )
        return False

    def reset(self) -> None:
        """Reset guard to initial state.

        Clears count and tripped flag.  Should be called on operator
        reset or new session start.
        """
        self._state = ConsecutiveLossState()
