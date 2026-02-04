"""Unit tests for retry utilities.

Tests cover:
- RetryPolicy validation and delay calculation
- is_retryable error classification
- retry_with_policy behavior
- Bounded-time tests (no real sleep)
- TransientFailureConfig injection in mock connector
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from grinder.connectors import (
    BinanceWsMockConnector,
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorTimeoutError,
    ConnectorTransientError,
    RetryPolicy,
    is_retryable,
    retry_with_policy,
)
from grinder.connectors.binance_ws_mock import TransientFailureConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


# --- Fixtures ---


@pytest.fixture
def sample_events() -> list[dict[str, Any]]:
    """Sample SNAPSHOT events for testing."""
    return [
        {
            "type": "SNAPSHOT",
            "ts": 1000,
            "symbol": "BTCUSDT",
            "bid_price": "50000.00",
            "ask_price": "50001.00",
            "bid_qty": "1.0",
            "ask_qty": "1.0",
            "last_price": "50000.50",
            "last_qty": "0.5",
        },
        {
            "type": "SNAPSHOT",
            "ts": 2000,
            "symbol": "BTCUSDT",
            "bid_price": "50100.00",
            "ask_price": "50101.00",
            "bid_qty": "1.5",
            "ask_qty": "1.5",
            "last_price": "50100.50",
            "last_qty": "0.3",
        },
    ]


@pytest.fixture
def fixture_path(sample_events: list[dict[str, Any]]) -> Iterator[Path]:
    """Create a temporary fixture directory with events.jsonl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        jsonl_path = path / "events.jsonl"
        with jsonl_path.open("w") as f:
            for event in sample_events:
                f.write(json.dumps(event) + "\n")
        yield path


# --- RetryPolicy Tests ---


class TestRetryPolicy:
    """Tests for RetryPolicy dataclass."""

    def test_default_values(self) -> None:
        """Default values are sensible."""
        policy = RetryPolicy()
        assert policy.max_attempts == 3
        assert policy.base_delay_ms == 100
        assert policy.max_delay_ms == 5000
        assert policy.backoff_multiplier == 2.0
        assert policy.retry_on_timeout is True

    def test_custom_values(self) -> None:
        """Custom values are stored correctly."""
        policy = RetryPolicy(
            max_attempts=5,
            base_delay_ms=200,
            max_delay_ms=10000,
            backoff_multiplier=1.5,
            retry_on_timeout=False,
        )
        assert policy.max_attempts == 5
        assert policy.base_delay_ms == 200
        assert policy.max_delay_ms == 10000
        assert policy.backoff_multiplier == 1.5
        assert policy.retry_on_timeout is False

    def test_validation_max_attempts_at_least_one(self) -> None:
        """max_attempts must be >= 1."""
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            RetryPolicy(max_attempts=0)

    def test_validation_base_delay_non_negative(self) -> None:
        """base_delay_ms must be >= 0."""
        with pytest.raises(ValueError, match="base_delay_ms must be >= 0"):
            RetryPolicy(base_delay_ms=-1)

    def test_validation_max_delay_gte_base(self) -> None:
        """max_delay_ms must be >= base_delay_ms."""
        with pytest.raises(ValueError, match="max_delay_ms must be >= base_delay_ms"):
            RetryPolicy(base_delay_ms=1000, max_delay_ms=500)

    def test_validation_backoff_multiplier_gte_one(self) -> None:
        """backoff_multiplier must be >= 1.0."""
        with pytest.raises(ValueError, match=r"backoff_multiplier must be >= 1\.0"):
            RetryPolicy(backoff_multiplier=0.5)

    def test_compute_delay_first_attempt(self) -> None:
        """First attempt (0) uses base delay."""
        policy = RetryPolicy(base_delay_ms=100, backoff_multiplier=2.0)
        assert policy.compute_delay_ms(0) == 100

    def test_compute_delay_exponential(self) -> None:
        """Delays increase exponentially."""
        policy = RetryPolicy(base_delay_ms=100, backoff_multiplier=2.0, max_delay_ms=10000)
        assert policy.compute_delay_ms(0) == 100
        assert policy.compute_delay_ms(1) == 200
        assert policy.compute_delay_ms(2) == 400
        assert policy.compute_delay_ms(3) == 800

    def test_compute_delay_caps_at_max(self) -> None:
        """Delay is capped at max_delay_ms."""
        policy = RetryPolicy(base_delay_ms=100, backoff_multiplier=2.0, max_delay_ms=500)
        assert policy.compute_delay_ms(10) == 500  # Would be 102400 without cap

    def test_frozen(self) -> None:
        """RetryPolicy is immutable (frozen)."""
        policy = RetryPolicy()
        with pytest.raises(AttributeError):
            policy.max_attempts = 10  # type: ignore[misc]


# --- is_retryable Tests ---


class TestIsRetryable:
    """Tests for is_retryable error classification."""

    def test_transient_error_is_retryable(self) -> None:
        """ConnectorTransientError is always retryable."""
        policy = RetryPolicy()
        error = ConnectorTransientError("connection reset")
        assert is_retryable(error, policy) is True

    def test_timeout_error_retryable_by_default(self) -> None:
        """ConnectorTimeoutError is retryable when retry_on_timeout=True."""
        policy = RetryPolicy(retry_on_timeout=True)
        error = ConnectorTimeoutError(op="connect", timeout_ms=5000)
        assert is_retryable(error, policy) is True

    def test_timeout_error_not_retryable_when_disabled(self) -> None:
        """ConnectorTimeoutError is not retryable when retry_on_timeout=False."""
        policy = RetryPolicy(retry_on_timeout=False)
        error = ConnectorTimeoutError(op="connect", timeout_ms=5000)
        assert is_retryable(error, policy) is False

    def test_non_retryable_error_never_retried(self) -> None:
        """ConnectorNonRetryableError is never retryable."""
        policy = RetryPolicy()
        error = ConnectorNonRetryableError("invalid credentials")
        assert is_retryable(error, policy) is False

    def test_closed_error_never_retried(self) -> None:
        """ConnectorClosedError is never retryable."""
        policy = RetryPolicy()
        error = ConnectorClosedError("connect")
        assert is_retryable(error, policy) is False

    def test_unknown_error_not_retried(self) -> None:
        """Unknown exceptions are not retried (fail fast)."""
        policy = RetryPolicy()
        error = ValueError("unexpected")
        assert is_retryable(error, policy) is False


# --- retry_with_policy Tests ---


class TestRetryWithPolicy:
    """Tests for retry_with_policy utility (bounded-time, no real sleep)."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        """Operation succeeds without retries."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            return "success"

        policy = RetryPolicy(max_attempts=3)
        result, stats = await retry_with_policy("test_op", operation, policy)

        assert result == "success"
        assert call_count == 1
        assert stats.attempts == 1
        assert stats.retries == 0
        assert stats.total_delay_ms == 0
        assert stats.last_error is None

    @pytest.mark.asyncio
    async def test_success_after_transient_failures(self) -> None:
        """Operation succeeds after transient failures."""
        call_count = 0
        delays: list[float] = []

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectorTransientError("temporary failure")
            return "success"

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        policy = RetryPolicy(max_attempts=5, base_delay_ms=100, backoff_multiplier=2.0)
        result, stats = await retry_with_policy("test_op", operation, policy, sleep_func=fake_sleep)

        assert result == "success"
        assert call_count == 3
        assert stats.attempts == 3
        assert stats.retries == 2
        assert len(delays) == 2
        # Verify exponential backoff: 100ms, 200ms
        assert delays == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_exhausts_retries_on_persistent_failure(self) -> None:
        """Raises last error after exhausting retries."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectorTransientError(f"failure #{call_count}")

        async def fake_sleep(delay: float) -> None:
            pass

        policy = RetryPolicy(max_attempts=3, base_delay_ms=100)

        with pytest.raises(ConnectorTransientError, match="failure #3"):
            await retry_with_policy("test_op", operation, policy, sleep_func=fake_sleep)

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_non_retryable_error(self) -> None:
        """Non-retryable errors fail immediately."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectorNonRetryableError("invalid")

        policy = RetryPolicy(max_attempts=5)

        with pytest.raises(ConnectorNonRetryableError, match="invalid"):
            await retry_with_policy("test_op", operation, policy)

        assert call_count == 1  # No retries

    @pytest.mark.asyncio
    async def test_no_retry_on_closed_error(self) -> None:
        """Closed errors fail immediately."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectorClosedError("connect")

        policy = RetryPolicy(max_attempts=5)

        with pytest.raises(ConnectorClosedError):
            await retry_with_policy("test_op", operation, policy)

        assert call_count == 1  # No retries

    @pytest.mark.asyncio
    async def test_timeout_error_retried_by_default(self) -> None:
        """Timeout errors are retried by default."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectorTimeoutError(op="connect", timeout_ms=5000)
            return "success"

        async def fake_sleep(delay: float) -> None:
            pass

        policy = RetryPolicy(max_attempts=3, retry_on_timeout=True)
        result, _stats = await retry_with_policy(
            "test_op", operation, policy, sleep_func=fake_sleep
        )

        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_error_not_retried_when_disabled(self) -> None:
        """Timeout errors fail immediately when retry_on_timeout=False."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectorTimeoutError(op="connect", timeout_ms=5000)

        policy = RetryPolicy(max_attempts=5, retry_on_timeout=False)

        with pytest.raises(ConnectorTimeoutError):
            await retry_with_policy("test_op", operation, policy)

        assert call_count == 1  # No retries

    @pytest.mark.asyncio
    async def test_on_retry_callback_invoked(self) -> None:
        """on_retry callback is called before each retry."""
        call_count = 0
        callback_calls: list[tuple[int, str, int]] = []

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectorTransientError(f"failure #{call_count}")
            return "success"

        async def fake_sleep(delay: float) -> None:
            pass

        def on_retry(attempt: int, error: Exception, delay_ms: int) -> None:
            callback_calls.append((attempt, str(error), delay_ms))

        policy = RetryPolicy(max_attempts=5, base_delay_ms=100, backoff_multiplier=2.0)
        await retry_with_policy(
            "test_op", operation, policy, sleep_func=fake_sleep, on_retry=on_retry
        )

        assert len(callback_calls) == 2
        assert callback_calls[0][0] == 0  # First retry (attempt 0)
        assert callback_calls[0][2] == 100  # 100ms delay
        assert callback_calls[1][0] == 1  # Second retry (attempt 1)
        assert callback_calls[1][2] == 200  # 200ms delay

    @pytest.mark.asyncio
    async def test_stats_track_all_errors(self) -> None:
        """Stats track all encountered errors."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectorTransientError(f"failure #{call_count}")
            return "success"

        async def fake_sleep(delay: float) -> None:
            pass

        policy = RetryPolicy(max_attempts=5, base_delay_ms=100)
        _, stats = await retry_with_policy("test_op", operation, policy, sleep_func=fake_sleep)

        assert len(stats.errors) == 2
        assert stats.last_error is not None
        assert "failure #2" in str(stats.last_error)

    @pytest.mark.asyncio
    async def test_single_attempt_no_retry(self) -> None:
        """max_attempts=1 means no retries."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectorTransientError("failure")

        policy = RetryPolicy(max_attempts=1)

        with pytest.raises(ConnectorTransientError):
            await retry_with_policy("test_op", operation, policy)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_total_delay_accumulates(self) -> None:
        """total_delay_ms accumulates across retries."""
        call_count = 0

        async def operation() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise ConnectorTransientError("failure")
            return "success"

        async def fake_sleep(delay: float) -> None:
            pass

        # base=100, multiplier=2 → delays: 100, 200, 400
        policy = RetryPolicy(max_attempts=5, base_delay_ms=100, backoff_multiplier=2.0)
        _, stats = await retry_with_policy("test_op", operation, policy, sleep_func=fake_sleep)

        assert stats.total_delay_ms == 100 + 200 + 400  # 700ms total


# --- TransientFailureConfig Tests ---


class TestTransientFailureConfig:
    """Tests for TransientFailureConfig in mock connector."""

    @pytest.mark.asyncio
    async def test_connect_failures_simulation(self, fixture_path: Path) -> None:
        """Mock connector simulates N connect failures."""
        config = TransientFailureConfig(connect_failures=2)
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)

        # First two connects should fail
        with pytest.raises(ConnectorTransientError):
            await connector.connect()
        with pytest.raises(ConnectorTransientError):
            await connector.connect()

        # Third connect should succeed
        await connector.connect()
        assert connector.stats.transient_failures_injected == 2
        await connector.close()

    @pytest.mark.asyncio
    async def test_read_failures_simulation(self, fixture_path: Path) -> None:
        """Mock connector simulates N read failures."""
        config = TransientFailureConfig(read_failures=2)
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)
        await connector.connect()

        errors_caught = 0
        snapshots_received = 0

        # Iterate and count failures
        while True:
            try:
                async for _snapshot in connector.iter_snapshots():
                    snapshots_received += 1
                break  # Normal iteration completed
            except ConnectorTransientError:
                errors_caught += 1
                if errors_caught > 10:  # Safety limit
                    break

        assert errors_caught == 2
        assert connector.stats.transient_failures_injected == 2
        await connector.close()

    @pytest.mark.asyncio
    async def test_custom_failure_message(self, fixture_path: Path) -> None:
        """Custom failure message is used."""
        config = TransientFailureConfig(connect_failures=1, failure_message="Custom error message")
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)

        with pytest.raises(ConnectorTransientError, match="Custom error message"):
            await connector.connect()

    @pytest.mark.asyncio
    async def test_no_failures_when_config_is_zero(self, fixture_path: Path) -> None:
        """No failures injected when counts are zero."""
        config = TransientFailureConfig(connect_failures=0, read_failures=0)
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)

        await connector.connect()
        count = 0
        async for _ in connector.iter_snapshots():
            count += 1

        assert count == 2
        assert connector.stats.transient_failures_injected == 0
        await connector.close()

    @pytest.mark.asyncio
    async def test_default_config_no_failures(self, fixture_path: Path) -> None:
        """Default TransientFailureConfig injects no failures."""
        connector = BinanceWsMockConnector(fixture_path)
        await connector.connect()

        count = 0
        async for _ in connector.iter_snapshots():
            count += 1

        assert count == 2
        assert connector.stats.transient_failures_injected == 0
        await connector.close()


# --- Integration Tests: Retry + Mock Connector ---


class TestRetryWithMockConnector:
    """Integration tests combining retry_with_policy and mock connector."""

    @pytest.mark.asyncio
    async def test_retry_recovers_from_connect_failures(self, fixture_path: Path) -> None:
        """retry_with_policy recovers from transient connect failures."""
        config = TransientFailureConfig(connect_failures=2)
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)

        async def connect_op() -> None:
            await connector.connect()

        async def fake_sleep(delay: float) -> None:
            pass

        policy = RetryPolicy(max_attempts=5, base_delay_ms=100)
        _, stats = await retry_with_policy("connect", connect_op, policy, sleep_func=fake_sleep)

        assert connector.state.value == "connected"
        assert stats.attempts == 3  # 2 failures + 1 success
        assert stats.retries == 2
        await connector.close()

    @pytest.mark.asyncio
    async def test_retry_exhausted_on_too_many_failures(self, fixture_path: Path) -> None:
        """Retry exhausted when failures exceed max_attempts."""
        config = TransientFailureConfig(connect_failures=10)
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)

        async def connect_op() -> None:
            await connector.connect()

        async def fake_sleep(delay: float) -> None:
            pass

        policy = RetryPolicy(max_attempts=3, base_delay_ms=100)

        with pytest.raises(ConnectorTransientError):
            await retry_with_policy("connect", connect_op, policy, sleep_func=fake_sleep)

        assert connector.stats.transient_failures_injected == 3
        await connector.close()

    @pytest.mark.asyncio
    async def test_bounded_time_test_pattern(self, fixture_path: Path) -> None:
        """Demonstrates bounded-time testing without real sleep."""
        config = TransientFailureConfig(connect_failures=3)
        connector = BinanceWsMockConnector(fixture_path, transient_failure_config=config)

        total_simulated_delay = 0.0

        async def connect_op() -> None:
            await connector.connect()

        async def fake_sleep(delay: float) -> None:
            nonlocal total_simulated_delay
            total_simulated_delay += delay

        policy = RetryPolicy(
            max_attempts=5,
            base_delay_ms=1000,
            backoff_multiplier=2.0,  # Would be 7s real
        )

        start = time.monotonic()
        _, stats = await retry_with_policy("connect", connect_op, policy, sleep_func=fake_sleep)
        elapsed = time.monotonic() - start

        # Real time should be fast (<100ms) despite simulating 7 seconds of delays
        assert elapsed < 0.1
        assert total_simulated_delay == 1.0 + 2.0 + 4.0  # 7 seconds simulated
        assert stats.total_delay_ms == 1000 + 2000 + 4000
        await connector.close()
