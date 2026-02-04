"""Tests for Circuit Breaker (H4).

Tests cover:
- State transitions: CLOSED → OPEN → HALF_OPEN → CLOSED
- Per-operation isolation
- Configurable thresholds
- Injectable clock for deterministic testing
- Fast-fail behavior (no underlying call when OPEN)
- trip_on callable for selective failure counting

See: ADR-027 for design decisions
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from grinder.connectors import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
    ConnectorTimeoutError,
    ConnectorTransientError,
    default_trip_on,
)


@dataclass
class FakeClock:
    """Fake clock for deterministic testing."""

    _time: float = 0.0

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


# --- Circuit State Tests ---


class TestCircuitStateTransitions:
    """Tests for circuit breaker state transitions."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def config(self) -> CircuitBreakerConfig:
        return CircuitBreakerConfig(
            failure_threshold=3,
            open_interval_s=30.0,
            half_open_probe_count=1,
            success_threshold=1,
        )

    @pytest.fixture
    def breaker(self, config: CircuitBreakerConfig, clock: FakeClock) -> CircuitBreaker:
        return CircuitBreaker(config=config, clock=clock)

    def test_initial_state_is_closed(self, breaker: CircuitBreaker) -> None:
        """Circuit starts in CLOSED state."""
        assert breaker.state("place") == CircuitState.CLOSED

    def test_failures_below_threshold_stays_closed(self, breaker: CircuitBreaker) -> None:
        """Failures below threshold keep circuit CLOSED."""
        # 2 failures, threshold is 3
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        assert breaker.state("place") == CircuitState.CLOSED

    def test_failures_at_threshold_opens_circuit(self, breaker: CircuitBreaker) -> None:
        """Reaching failure threshold opens circuit."""
        for i in range(3):
            breaker.record_failure("place", f"error{i}")

        assert breaker.state("place") == CircuitState.OPEN

    def test_success_resets_failure_count(self, breaker: CircuitBreaker) -> None:
        """Success in CLOSED state resets consecutive failures."""
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")
        breaker.record_success("place")  # Reset failures

        # Now we need 3 more failures to open
        breaker.record_failure("place", "error3")
        breaker.record_failure("place", "error4")
        assert breaker.state("place") == CircuitState.CLOSED

        breaker.record_failure("place", "error5")
        assert breaker.state("place") == CircuitState.OPEN

    def test_open_transitions_to_half_open_after_interval(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        """OPEN → HALF_OPEN after open_interval_s."""
        # Trip the breaker
        for i in range(3):
            breaker.record_failure("place", f"error{i}")
        assert breaker.state("place") == CircuitState.OPEN

        # Before interval - still OPEN
        clock.advance(29.9)
        assert breaker.state("place") == CircuitState.OPEN

        # After interval - HALF_OPEN
        clock.advance(0.2)
        assert breaker.state("place") == CircuitState.HALF_OPEN

    def test_half_open_success_closes_circuit(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        """Success in HALF_OPEN closes circuit."""
        # Trip the breaker
        for i in range(3):
            breaker.record_failure("place", f"error{i}")

        # Wait for HALF_OPEN
        clock.advance(30.1)
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # Success closes it
        breaker.record_success("place")
        assert breaker.state("place") == CircuitState.CLOSED

    def test_half_open_failure_reopens_circuit(
        self, breaker: CircuitBreaker, clock: FakeClock
    ) -> None:
        """Failure in HALF_OPEN reopens circuit."""
        # Trip the breaker
        for i in range(3):
            breaker.record_failure("place", f"error{i}")

        # Wait for HALF_OPEN
        clock.advance(30.1)
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # Failure reopens it
        breaker.record_failure("place", "probe_failed")
        assert breaker.state("place") == CircuitState.OPEN

    def test_half_open_requires_success_threshold(self, clock: FakeClock) -> None:
        """HALF_OPEN requires success_threshold consecutive successes."""
        config = CircuitBreakerConfig(
            failure_threshold=2,
            open_interval_s=10.0,
            half_open_probe_count=3,
            success_threshold=2,  # Need 2 successes
        )
        breaker = CircuitBreaker(config=config, clock=clock)

        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        # Wait for HALF_OPEN
        clock.advance(10.1)
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # One success - still HALF_OPEN
        breaker.record_success("place")
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # Second success - now CLOSED
        breaker.record_success("place")
        assert breaker.state("place") == CircuitState.CLOSED


# --- Fast-Fail Behavior Tests ---


class TestFastFailBehavior:
    """Tests for fast-fail when circuit is OPEN."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def breaker(self, clock: FakeClock) -> CircuitBreaker:
        config = CircuitBreakerConfig(
            failure_threshold=2,
            open_interval_s=30.0,
            half_open_probe_count=1,
        )
        return CircuitBreaker(config=config, clock=clock)

    def test_open_circuit_rejects_calls(self, breaker: CircuitBreaker) -> None:
        """OPEN circuit rejects allow() calls."""
        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")
        assert breaker.state("place") == CircuitState.OPEN

        # Should reject
        assert breaker.allow("place") is False
        assert breaker.stats.rejected_calls == 1

    def test_before_call_raises_circuit_open_error(self, breaker: CircuitBreaker) -> None:
        """before_call() raises CircuitOpenError when OPEN."""
        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        with pytest.raises(CircuitOpenError) as exc_info:
            breaker.before_call("place")

        assert exc_info.value.op_name == "place"
        assert exc_info.value.circuit_state == "OPEN"

    def test_open_does_not_call_underlying_operation(self, breaker: CircuitBreaker) -> None:
        """When OPEN, underlying operation should NOT be called.

        This test verifies the pattern: check breaker BEFORE calling operation.
        """
        call_count = 0

        def mock_operation() -> str:
            nonlocal call_count
            call_count += 1
            return "success"

        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        # Pattern: check breaker, only call if allowed
        if breaker.allow("place"):
            mock_operation()

        assert call_count == 0  # Operation was NOT called

    def test_half_open_limits_probes(self, breaker: CircuitBreaker, clock: FakeClock) -> None:
        """HALF_OPEN allows limited probes, then rejects."""
        # Trip the breaker
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        # Wait for HALF_OPEN
        clock.advance(30.1)
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # First probe allowed (half_open_probe_count=1)
        assert breaker.allow("place") is True

        # Second probe rejected
        assert breaker.allow("place") is False
        assert breaker.stats.rejected_calls == 1


# --- Per-Operation Isolation Tests ---


class TestPerOperationIsolation:
    """Tests for per-operation circuit breaker isolation."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def breaker(self, clock: FakeClock) -> CircuitBreaker:
        config = CircuitBreakerConfig(
            failure_threshold=2,
            open_interval_s=30.0,
        )
        return CircuitBreaker(config=config, clock=clock)

    def test_operations_have_independent_state(self, breaker: CircuitBreaker) -> None:
        """Different operations have independent circuit state."""
        # Trip 'place' circuit
        breaker.record_failure("place", "error1")
        breaker.record_failure("place", "error2")

        # 'place' is OPEN, but 'cancel' is still CLOSED
        assert breaker.state("place") == CircuitState.OPEN
        assert breaker.state("cancel") == CircuitState.CLOSED

        # Can still use 'cancel'
        assert breaker.allow("cancel") is True

    def test_operations_trip_independently(self, breaker: CircuitBreaker) -> None:
        """Each operation must exceed its own failure threshold."""
        # One failure each - both stay CLOSED
        breaker.record_failure("place", "error")
        breaker.record_failure("cancel", "error")

        assert breaker.state("place") == CircuitState.CLOSED
        assert breaker.state("cancel") == CircuitState.CLOSED

        # Second failure for 'place' only
        breaker.record_failure("place", "error2")

        assert breaker.state("place") == CircuitState.OPEN
        assert breaker.state("cancel") == CircuitState.CLOSED

    def test_reset_single_operation(self, breaker: CircuitBreaker) -> None:
        """reset(op_name) only resets that operation."""
        # Trip both circuits
        breaker.record_failure("place", "e1")
        breaker.record_failure("place", "e2")
        breaker.record_failure("cancel", "e1")
        breaker.record_failure("cancel", "e2")

        assert breaker.state("place") == CircuitState.OPEN
        assert breaker.state("cancel") == CircuitState.OPEN

        # Reset only 'place'
        breaker.reset("place")

        assert breaker.state("place") == CircuitState.CLOSED
        assert breaker.state("cancel") == CircuitState.OPEN

    def test_reset_all_operations(self, breaker: CircuitBreaker) -> None:
        """reset() without args resets all operations."""
        # Trip both circuits
        breaker.record_failure("place", "e1")
        breaker.record_failure("place", "e2")
        breaker.record_failure("cancel", "e1")
        breaker.record_failure("cancel", "e2")

        # Reset all
        breaker.reset()

        assert breaker.state("place") == CircuitState.CLOSED
        assert breaker.state("cancel") == CircuitState.CLOSED


# --- trip_on Callable Tests ---


class TestTripOnCallable:
    """Tests for selective failure counting via trip_on."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    def test_default_trip_on_counts_transient_errors(self) -> None:
        """default_trip_on counts transient errors."""
        assert default_trip_on(ConnectorTransientError("network error")) is True
        assert default_trip_on(ConnectorTimeoutError("read", 5000)) is True

    def test_default_trip_on_ignores_other_errors(self) -> None:
        """default_trip_on ignores non-transient errors."""
        from grinder.connectors import (  # noqa: PLC0415
            ConnectorNonRetryableError,
            IdempotencyConflictError,
        )

        assert default_trip_on(ConnectorNonRetryableError("bad request")) is False
        assert default_trip_on(IdempotencyConflictError("key", "INFLIGHT")) is False
        assert default_trip_on(ValueError("random error")) is False

    def test_custom_trip_on(self, clock: FakeClock) -> None:
        """Custom trip_on allows selective failure counting."""

        def trip_only_on_timeout(error: Exception) -> bool:
            return isinstance(error, ConnectorTimeoutError)

        config = CircuitBreakerConfig(
            failure_threshold=2,
            trip_on=trip_only_on_timeout,
        )
        _breaker = CircuitBreaker(config=config, clock=clock)

        # Transient errors don't trip (because custom trip_on ignores them)
        # We need to use the trip_on check manually in real integration
        # For this test, we just verify the callable works
        assert config.trip_on(ConnectorTransientError("error")) is False
        assert config.trip_on(ConnectorTimeoutError("read", 5000)) is True


# --- Statistics Tests ---


class TestCircuitBreakerStats:
    """Tests for circuit breaker statistics."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def breaker(self, clock: FakeClock) -> CircuitBreaker:
        config = CircuitBreakerConfig(failure_threshold=2)
        return CircuitBreaker(config=config, clock=clock)

    def test_stats_track_calls(self, breaker: CircuitBreaker) -> None:
        """Stats track total, successful, and failed calls."""
        breaker.allow("place")  # total_calls += 1
        breaker.record_success("place")  # successful_calls += 1

        breaker.allow("place")  # total_calls += 1
        breaker.record_failure("place", "err")  # failed_calls += 1

        stats = breaker.stats
        assert stats.total_calls == 2
        assert stats.successful_calls == 1
        assert stats.failed_calls == 1

    def test_stats_track_rejections(self, breaker: CircuitBreaker) -> None:
        """Stats track rejected calls when OPEN."""
        # Trip the breaker
        breaker.record_failure("place", "e1")
        breaker.record_failure("place", "e2")

        # Try to call - rejected
        breaker.allow("place")  # rejected
        breaker.allow("place")  # rejected

        assert breaker.stats.rejected_calls == 2

    def test_stats_track_state_transitions(self, breaker: CircuitBreaker, clock: FakeClock) -> None:
        """Stats track state transitions."""
        # CLOSED → OPEN
        breaker.record_failure("place", "e1")
        breaker.record_failure("place", "e2")
        assert breaker.stats.state_transitions == 1

        # OPEN → HALF_OPEN
        clock.advance(30.1)
        breaker.state("place")  # Trigger transition check
        assert breaker.stats.state_transitions == 2

        # HALF_OPEN → CLOSED
        breaker.allow("place")
        breaker.record_success("place")
        assert breaker.stats.state_transitions == 3


# --- Integration Pattern Tests ---


class TestIntegrationPattern:
    """Tests for recommended integration pattern."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def breaker(self, clock: FakeClock) -> CircuitBreaker:
        config = CircuitBreakerConfig(
            failure_threshold=2,
            open_interval_s=30.0,
            trip_on=default_trip_on,
        )
        return CircuitBreaker(config=config, clock=clock)

    def test_full_lifecycle_pattern(self, breaker: CircuitBreaker, clock: FakeClock) -> None:
        """Test complete circuit breaker lifecycle.

        CLOSED → (failures) → OPEN → (wait) → HALF_OPEN → (success) → CLOSED
        """
        operation_results: list[str] = []

        def mock_operation(should_fail: bool) -> str:
            if should_fail:
                raise ConnectorTransientError("upstream down")
            return "success"

        # Phase 1: Normal operation (CLOSED)
        assert breaker.state("place") == CircuitState.CLOSED

        # Phase 2: Failures trip the circuit
        for _ in range(2):
            breaker.before_call("place")  # Allowed
            try:
                mock_operation(should_fail=True)
            except ConnectorTransientError as e:
                if breaker.should_trip(e):
                    breaker.record_failure("place", str(e))

        assert breaker.state("place") == CircuitState.OPEN

        # Phase 3: OPEN rejects calls
        with pytest.raises(CircuitOpenError):
            breaker.before_call("place")

        # Phase 4: Wait for HALF_OPEN
        clock.advance(30.1)
        assert breaker.state("place") == CircuitState.HALF_OPEN

        # Phase 5: Probe succeeds → CLOSED
        breaker.before_call("place")  # Probe allowed
        result = mock_operation(should_fail=False)
        operation_results.append(result)
        breaker.record_success("place")

        assert breaker.state("place") == CircuitState.CLOSED
        assert operation_results == ["success"]

    def test_inject_fail_first_n_times(self, breaker: CircuitBreaker, clock: FakeClock) -> None:
        """Mock scenario: fail first N times, then succeed.

        This is the P1 requirement from DoD.
        """
        call_count = 0
        fail_count = 3  # First 3 calls fail

        def flaky_operation() -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= fail_count:
                raise ConnectorTransientError(f"fail #{call_count}")
            return f"success #{call_count}"

        results: list[str] = []
        errors: list[str] = []

        # Attempt operations
        for _ in range(5):
            try:
                breaker.before_call("place")
                result = flaky_operation()
                breaker.record_success("place")
                results.append(result)
            except CircuitOpenError:
                errors.append("circuit_open")
            except ConnectorTransientError as e:
                if breaker.should_trip(e):
                    breaker.record_failure("place", str(e))
                errors.append(str(e))

        # After 2 failures, circuit opens
        # 3rd attempt hits CircuitOpenError (not the flaky op)
        # 4th and 5th also hit CircuitOpenError
        assert breaker.state("place") == CircuitState.OPEN
        assert call_count == 2  # Only 2 actual calls before circuit opened
        assert "circuit_open" in errors

        # Reset and try a cleaner scenario where probe succeeds
        breaker.reset()
        call_count = 0
        results = []
        errors = []

        # Now: fail 2 times (threshold), wait, then succeed on probe
        for _i in range(2):
            try:
                breaker.before_call("place")
                flaky_operation()  # fails
                breaker.record_success("place")
            except ConnectorTransientError as e:
                breaker.record_failure("place", str(e))
                errors.append(str(e))

        assert breaker.state("place") == CircuitState.OPEN

        # Wait and probe
        clock.advance(30.1)
        call_count = 10  # Skip past failures

        breaker.before_call("place")
        result = flaky_operation()  # succeeds
        breaker.record_success("place")
        results.append(result)

        assert breaker.state("place") == CircuitState.CLOSED
        assert len(results) == 1
