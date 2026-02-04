"""Retry utilities for connector operations.

Provides centralized retry logic with exponential backoff for transient failures.
Designed for deterministic testing (no jitter in tests, fake clock support).

Usage:
    policy = RetryPolicy(max_attempts=3, base_delay_ms=100)
    result = await retry_with_policy(
        "connect",
        lambda: connector.connect(),
        policy,
        classify_error,
    )
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from grinder.connectors.errors import (
    ConnectorClosedError,
    ConnectorNonRetryableError,
    ConnectorTimeoutError,
    ConnectorTransientError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for retry behavior.

    Attributes:
        max_attempts: Maximum number of attempts (1 = no retries, default 3)
        base_delay_ms: Initial delay between retries in milliseconds (default 100)
        max_delay_ms: Maximum delay cap in milliseconds (default 5000)
        backoff_multiplier: Multiplier for exponential backoff (default 2.0)
        retry_on_timeout: Whether to retry on ConnectorTimeoutError (default True)
    """

    max_attempts: int = 3
    base_delay_ms: int = 100
    max_delay_ms: int = 5000
    backoff_multiplier: float = 2.0
    retry_on_timeout: bool = True

    def __post_init__(self) -> None:
        """Validate policy parameters."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_ms < 0:
            raise ValueError("base_delay_ms must be >= 0")
        if self.max_delay_ms < self.base_delay_ms:
            raise ValueError("max_delay_ms must be >= base_delay_ms")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be >= 1.0")

    def compute_delay_ms(self, attempt: int) -> int:
        """Compute delay for given attempt number (0-indexed).

        Returns delay in milliseconds, capped at max_delay_ms.
        """
        delay = self.base_delay_ms * (self.backoff_multiplier**attempt)
        return min(int(delay), self.max_delay_ms)


@dataclass
class RetryStats:
    """Statistics from a retry operation.

    Attributes:
        attempts: Total number of attempts made
        retries: Number of retries (attempts - 1)
        total_delay_ms: Total time spent in delays
        last_error: Last error encountered (if any)
        errors: List of all errors encountered
    """

    attempts: int = 0
    retries: int = 0
    total_delay_ms: int = 0
    last_error: Exception | None = None
    errors: list[Exception] = field(default_factory=list)


def is_retryable(error: Exception, policy: RetryPolicy) -> bool:
    """Determine if an error is retryable based on policy.

    Classification:
    - ConnectorTransientError: Always retryable
    - ConnectorTimeoutError: Retryable if policy.retry_on_timeout is True
    - ConnectorNonRetryableError: Never retryable
    - ConnectorClosedError: Never retryable
    - Other exceptions: Never retryable (fail fast)
    """
    if isinstance(error, ConnectorTransientError):
        return True
    if isinstance(error, ConnectorTimeoutError):
        return policy.retry_on_timeout
    if isinstance(error, (ConnectorNonRetryableError, ConnectorClosedError)):
        return False
    # Unknown errors are not retried by default (fail fast)
    return False


async def retry_with_policy(
    op_name: str,
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep_func: Callable[[float], Awaitable[None]] | None = None,
    on_retry: Callable[[int, Exception, int], None] | None = None,
) -> tuple[T, RetryStats]:
    """Execute operation with retry policy.

    Args:
        op_name: Name of operation for logging
        operation: Async callable to execute
        policy: Retry policy configuration
        sleep_func: Optional custom sleep function for testing (default asyncio.sleep)
        on_retry: Optional callback(attempt, error, delay_ms) called before each retry

    Returns:
        Tuple of (result, stats) where result is the operation result
        and stats contains retry statistics.

    Raises:
        The last exception if all retries are exhausted or error is non-retryable.
    """
    if sleep_func is None:
        sleep_func = asyncio.sleep

    stats = RetryStats()

    for attempt in range(policy.max_attempts):
        stats.attempts = attempt + 1
        try:
            result = await operation()
            return result, stats
        except Exception as e:
            stats.errors.append(e)
            stats.last_error = e

            # Check if we should retry
            if not is_retryable(e, policy):
                logger.debug(
                    "Non-retryable error during %s (attempt %d): %s",
                    op_name,
                    attempt + 1,
                    e,
                )
                raise

            # Check if we have more attempts
            if attempt + 1 >= policy.max_attempts:
                logger.warning(
                    "Max retries exhausted for %s after %d attempts: %s",
                    op_name,
                    policy.max_attempts,
                    e,
                )
                raise

            # Compute delay and wait
            delay_ms = policy.compute_delay_ms(attempt)
            stats.retries += 1
            stats.total_delay_ms += delay_ms

            logger.debug(
                "Retrying %s (attempt %d/%d) after %dms: %s",
                op_name,
                attempt + 1,
                policy.max_attempts,
                delay_ms,
                e,
            )

            if on_retry:
                on_retry(attempt, e, delay_ms)

            await sleep_func(delay_ms / 1000.0)

    # Should never reach here, but satisfy type checker
    raise RuntimeError("Unexpected end of retry loop")  # pragma: no cover
