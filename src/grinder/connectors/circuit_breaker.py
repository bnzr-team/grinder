"""Circuit breaker for connector operations.

Prevents cascading failures by fast-failing when upstream is degraded.

States:
- CLOSED: Normal operation, failures counted
- OPEN: Fast-fail all requests, cooldown timer running
- HALF_OPEN: Allow limited probes, success → CLOSED, failure → OPEN

Key design decisions (see ADR-027):
- Per-operation tracking: place/cancel/replace can trip independently
- Injectable clock for deterministic testing
- trip_on callable determines which errors count as breaker-worthy
- Integrates BEFORE retries (breaker fast-fail is non-retryable)

Usage:
    breaker = CircuitBreaker(config)

    # Before each operation
    if not breaker.allow("place"):
        raise CircuitOpenError("place")

    try:
        result = await do_operation()
        breaker.record_success("place")
        return result
    except SomeError as e:
        if config.trip_on(e):
            breaker.record_failure("place", str(e))
        raise

See: ADR-027 for design decisions
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class CircuitState(Enum):
    """State of a circuit breaker."""

    CLOSED = "CLOSED"  # Normal operation, failures counted
    OPEN = "OPEN"  # Fast-fail, cooldown timer running
    HALF_OPEN = "HALF_OPEN"  # Limited probes allowed


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker.

    Attributes:
        failure_threshold: Consecutive failures to trip OPEN (default: 5)
        open_interval_s: Seconds to stay OPEN before HALF_OPEN (default: 30)
        half_open_probe_count: Max probes allowed in HALF_OPEN (default: 1)
        success_threshold: Consecutive successes in HALF_OPEN to close (default: 1)
        trip_on: Callable to determine if error should count as failure
    """

    failure_threshold: int = 5
    open_interval_s: float = 30.0
    half_open_probe_count: int = 1
    success_threshold: int = 1
    trip_on: Callable[[Exception], bool] = field(default=lambda _e: True)


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker.

    Attributes:
        total_calls: Total calls attempted
        successful_calls: Calls that succeeded
        failed_calls: Calls that failed (breaker-worthy)
        rejected_calls: Calls rejected due to OPEN state
        state_transitions: Number of state changes
    """

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_transitions: int = 0


@dataclass
class _OperationState:
    """Per-operation circuit breaker state.

    Attributes:
        state: Current circuit state
        consecutive_failures: Failures since last success
        consecutive_successes: Successes since last failure (for HALF_OPEN)
        opened_at: Timestamp when transitioned to OPEN
        half_open_probes: Number of probes attempted in current HALF_OPEN
    """

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float = 0.0
    half_open_probes: int = 0


class CircuitBreaker:
    """Circuit breaker with per-operation tracking.

    Thread-safe via lock. Each operation (place/cancel/replace/etc.)
    has independent state, so one operation can be OPEN while others
    remain CLOSED.

    Attributes:
        config: CircuitBreakerConfig
        _states: Per-operation state tracking
        _lock: Thread safety lock
        _clock: Injectable clock for testing
        _stats: Global statistics
    """

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        clock: Any = None,
    ) -> None:
        """Initialize circuit breaker.

        Args:
            config: Configuration (uses defaults if None)
            clock: Injectable clock with .time() method (uses time module if None)
        """
        self._config = config or CircuitBreakerConfig()
        self._clock = clock if clock is not None else time
        self._states: dict[str, _OperationState] = {}
        self._lock = threading.Lock()
        self._stats = CircuitBreakerStats()

    def _now(self) -> float:
        """Get current time from clock."""
        return float(self._clock.time())

    def _get_state(self, op_name: str) -> _OperationState:
        """Get or create operation state."""
        if op_name not in self._states:
            self._states[op_name] = _OperationState()
        return self._states[op_name]

    def state(self, op_name: str) -> CircuitState:
        """Get current state for operation.

        Also handles automatic transition from OPEN → HALF_OPEN
        when open_interval has elapsed.
        """
        with self._lock:
            op_state = self._get_state(op_name)
            self._maybe_transition_to_half_open(op_state)
            return op_state.state

    def allow(self, op_name: str) -> bool:
        """Check if operation is allowed.

        Returns True if operation should proceed, False if circuit is OPEN
        and should fast-fail.

        In HALF_OPEN state, allows limited probes up to half_open_probe_count.
        """
        with self._lock:
            self._stats.total_calls += 1
            op_state = self._get_state(op_name)

            # Check for OPEN → HALF_OPEN transition
            self._maybe_transition_to_half_open(op_state)

            if op_state.state == CircuitState.CLOSED:
                return True

            if op_state.state == CircuitState.OPEN:
                self._stats.rejected_calls += 1
                return False

            # HALF_OPEN: allow limited probes
            if op_state.half_open_probes < self._config.half_open_probe_count:
                op_state.half_open_probes += 1
                return True

            # Too many probes in HALF_OPEN, reject
            self._stats.rejected_calls += 1
            return False

    def before_call(self, op_name: str) -> None:
        """Check if operation is allowed, raise if not.

        This is the preferred method for integration - it raises
        CircuitOpenError which is non-retryable.

        Raises:
            CircuitOpenError: If circuit is OPEN or HALF_OPEN limit reached
        """
        if not self.allow(op_name):
            from grinder.connectors.errors import CircuitOpenError  # noqa: PLC0415

            raise CircuitOpenError(op_name, self.state(op_name).value)

    def record_success(self, op_name: str) -> None:
        """Record successful operation.

        In CLOSED state: resets consecutive failures.
        In HALF_OPEN state: counts toward success_threshold for closing.
        """
        with self._lock:
            self._stats.successful_calls += 1
            op_state = self._get_state(op_name)

            if op_state.state == CircuitState.CLOSED:
                # Reset failure count on success
                op_state.consecutive_failures = 0
                return

            if op_state.state == CircuitState.HALF_OPEN:
                op_state.consecutive_successes += 1
                if op_state.consecutive_successes >= self._config.success_threshold:
                    # Enough successes, close the circuit
                    self._transition_to_closed(op_state)

    def record_failure(self, op_name: str, reason: str = "") -> None:  # noqa: ARG002
        """Record failed operation.

        In CLOSED state: counts toward failure_threshold for opening.
        In HALF_OPEN state: immediately transitions back to OPEN.

        Args:
            op_name: Operation name
            reason: Optional failure reason (for logging/metrics)
        """
        with self._lock:
            self._stats.failed_calls += 1
            op_state = self._get_state(op_name)

            if op_state.state == CircuitState.CLOSED:
                op_state.consecutive_failures += 1
                op_state.consecutive_successes = 0

                if op_state.consecutive_failures >= self._config.failure_threshold:
                    self._transition_to_open(op_state)
                return

            if op_state.state == CircuitState.HALF_OPEN:
                # Any failure in HALF_OPEN → back to OPEN
                self._transition_to_open(op_state)

    def _maybe_transition_to_half_open(self, op_state: _OperationState) -> None:
        """Transition from OPEN → HALF_OPEN if cooldown elapsed.

        Must be called while holding lock.
        """
        if op_state.state != CircuitState.OPEN:
            return

        elapsed = self._now() - op_state.opened_at
        if elapsed >= self._config.open_interval_s:
            op_state.state = CircuitState.HALF_OPEN
            op_state.half_open_probes = 0
            op_state.consecutive_successes = 0
            self._stats.state_transitions += 1

    def _transition_to_open(self, op_state: _OperationState) -> None:
        """Transition to OPEN state.

        Must be called while holding lock.
        """
        op_state.state = CircuitState.OPEN
        op_state.opened_at = self._now()
        op_state.half_open_probes = 0
        op_state.consecutive_successes = 0
        self._stats.state_transitions += 1

    def _transition_to_closed(self, op_state: _OperationState) -> None:
        """Transition to CLOSED state.

        Must be called while holding lock.
        """
        op_state.state = CircuitState.CLOSED
        op_state.consecutive_failures = 0
        op_state.consecutive_successes = 0
        op_state.half_open_probes = 0
        self._stats.state_transitions += 1

    @property
    def stats(self) -> CircuitBreakerStats:
        """Get current statistics."""
        with self._lock:
            return CircuitBreakerStats(
                total_calls=self._stats.total_calls,
                successful_calls=self._stats.successful_calls,
                failed_calls=self._stats.failed_calls,
                rejected_calls=self._stats.rejected_calls,
                state_transitions=self._stats.state_transitions,
            )

    def reset(self, op_name: str | None = None) -> None:
        """Reset circuit breaker state.

        Args:
            op_name: If provided, reset only this operation. Otherwise reset all.
        """
        with self._lock:
            if op_name is not None:
                if op_name in self._states:
                    self._states[op_name] = _OperationState()
            else:
                self._states.clear()
                self._stats = CircuitBreakerStats()


def default_trip_on(error: Exception) -> bool:
    """Default trip_on function: trip on transient errors only.

    Trips on:
    - ConnectorTransientError (network issues, 5xx, 429)
    - ConnectorTimeoutError

    Does NOT trip on:
    - ConnectorNonRetryableError (4xx, auth, validation)
    - IdempotencyConflictError (not an upstream failure)
    """
    from grinder.connectors.errors import (  # noqa: PLC0415
        ConnectorTimeoutError,
        ConnectorTransientError,
    )

    return isinstance(error, (ConnectorTransientError, ConnectorTimeoutError))
