"""Fill probability gate for SOR-level order blocking (Track C, PR-C5).

Pure function: ``check_fill_prob(model, features, threshold, enforce)``
returns ``FillProbResult`` with verdict ALLOW / BLOCK / SHADOW.

Behaviour:
- **Fail-open**: ``model=None`` → ALLOW (prob_bps=0, never blocks).
- **Shadow** (enforce=False): prediction computed, never blocks.
- **Enforce** (enforce=True): prob >= threshold → ALLOW, else BLOCK.

Gate applies only to PLACE/AMEND (risk-increasing) decisions.
CANCEL and NOOP always pass through without checking.

Circuit breaker (PR-C8, ADR-073):
- ``FillProbCircuitBreaker``: rolling-window block rate tracker.
- Trips when block rate exceeds ``max_block_rate_pct`` within ``window_seconds``.
- On trip: ALLOW (bypass), structured log, counter metric.
- Shadow mode: zero overhead (no recording).
- Fail-open: any internal error → ALLOW.

SSOT: this module.  ADR-071 + ADR-073 in docs/DECISIONS.md.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grinder.ml.fill_model_v0 import FillModelFeaturesV0, FillModelV0

logger = logging.getLogger(__name__)


class FillProbVerdict(StrEnum):
    """Verdict from fill probability gate."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    SHADOW = "SHADOW"


@dataclass(frozen=True)
class FillProbResult:
    """Result of a fill probability gate check.

    Attributes:
        verdict: Gate decision (ALLOW / BLOCK / SHADOW).
        prob_bps: Predicted fill probability in bps (0..10000), 0 if model unavailable.
        threshold_bps: Configured minimum threshold in bps.
        enforce: Whether enforcement was active for this check.
    """

    verdict: FillProbVerdict
    prob_bps: int
    threshold_bps: int
    enforce: bool


def check_fill_prob(
    *,
    model: FillModelV0 | None,
    features: FillModelFeaturesV0,
    threshold_bps: int = 2500,
    enforce: bool = False,
) -> FillProbResult:
    """Check fill probability gate.

    Args:
        model: Loaded FillModelV0, or None if unavailable.
        features: Entry-side features for prediction.
        threshold_bps: Minimum fill probability in bps (0..10000).
        enforce: If True, block orders below threshold.

    Returns:
        FillProbResult with verdict and prediction details.
    """
    # Fail-open: no model → always allow
    if model is None:
        return FillProbResult(
            verdict=FillProbVerdict.ALLOW,
            prob_bps=0,
            threshold_bps=threshold_bps,
            enforce=enforce,
        )

    prob_bps = model.predict(features)

    # Shadow mode: predict but never block
    if not enforce:
        return FillProbResult(
            verdict=FillProbVerdict.SHADOW,
            prob_bps=prob_bps,
            threshold_bps=threshold_bps,
            enforce=False,
        )

    # Enforce mode: block if below threshold
    if prob_bps >= threshold_bps:
        return FillProbResult(
            verdict=FillProbVerdict.ALLOW,
            prob_bps=prob_bps,
            threshold_bps=threshold_bps,
            enforce=True,
        )

    return FillProbResult(
        verdict=FillProbVerdict.BLOCK,
        prob_bps=prob_bps,
        threshold_bps=threshold_bps,
        enforce=True,
    )


# --- Circuit breaker (PR-C8, ADR-073) ----------------------------------------


class FillProbCircuitBreaker:
    """Rolling-window circuit breaker for fill probability gate.

    Tracks block rate over a sliding time window.  When block rate
    exceeds ``max_block_rate_pct``, the breaker trips: all subsequent
    gate checks bypass to ALLOW until the window rolls over.

    Design:
    - **Shadow-safe**: ``record()`` is a no-op when ``enforce=False``.
    - **Fail-open**: any internal error → not tripped (ALLOW).
    - Uses ``time.monotonic()`` for clock-skew resilience.
    """

    def __init__(
        self,
        window_seconds: int = 300,
        max_block_rate_pct: int = 50,
    ) -> None:
        self._window_seconds = max(1, window_seconds)
        self._max_block_rate_pct = max(0, min(100, max_block_rate_pct))
        # deque of (monotonic_ts, is_block: bool)
        self._events: deque[tuple[float, bool]] = deque()
        self._tripped = False
        self._trip_count = 0

    @property
    def trip_count(self) -> int:
        """Total number of times the circuit breaker has tripped."""
        return self._trip_count

    def record(self, verdict: FillProbVerdict, *, enforce: bool) -> None:
        """Record a gate decision.  No-op in shadow mode.

        Args:
            verdict: Gate verdict (ALLOW / BLOCK / SHADOW).
            enforce: Whether enforcement was active.
        """
        if not enforce:
            return

        now = time.monotonic()
        is_block = verdict == FillProbVerdict.BLOCK
        self._events.append((now, is_block))
        self._prune(now)

        # Check if we should trip
        if not self._tripped:
            total = len(self._events)
            if total > 0:
                blocks = sum(1 for _, b in self._events if b)
                rate_pct = blocks * 100 // total
                if rate_pct > self._max_block_rate_pct:
                    self._tripped = True
                    self._trip_count += 1
                    self._emit_trip(blocks, total, rate_pct)

    def is_tripped(self) -> bool:
        """Check if circuit breaker is currently tripped.

        Returns False (fail-open) on any internal error.
        """
        try:
            now = time.monotonic()
            self._prune(now)

            if not self._tripped:
                return False

            # Reset trip if window has rolled over and rate is now below threshold
            total = len(self._events)
            if total == 0:
                self._tripped = False
                return False

            blocks = sum(1 for _, b in self._events if b)
            rate_pct = blocks * 100 // total
            if rate_pct <= self._max_block_rate_pct:
                self._tripped = False
                return False

            return True
        except Exception:
            logger.warning(
                "FILL_PROB_CB_ERROR fail_open=true",
                exc_info=True,
            )
            return False

    def _prune(self, now: float) -> None:
        """Remove events older than the window."""
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _emit_trip(self, block_count: int, total_count: int, rate_pct: int) -> None:
        """Emit structured log on circuit breaker trip."""
        logger.warning(
            "FILL_PROB_CIRCUIT_BREAKER_TRIPPED block_count=%d total_count=%d "
            "block_rate_pct=%d window_seconds=%d",
            block_count,
            total_count,
            rate_pct,
            self._window_seconds,
            extra={
                "block_count": block_count,
                "total_count": total_count,
                "block_rate_pct": rate_pct,
                "window_seconds": self._window_seconds,
            },
        )
