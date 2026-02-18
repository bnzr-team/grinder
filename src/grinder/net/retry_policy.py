"""HTTP retry policy and per-op deadline budgets (Launch-05 PR1).

Provides:
- ``HttpRetryPolicy``: frozen config for HTTP-specific retries.
- ``DeadlinePolicy``: per-op deadline budgets (cancel_order=600ms, etc.).
- ``classify_http_error()``: maps HTTP/transport errors to stable reason strings.
- ``is_http_retryable()``: determines if a request should be retried.

Design:
- Safe-by-default: retries disabled (max_attempts=1) until ``LATENCY_RETRY_ENABLED=1``.
- Deterministic: jitter=False by default so tests are reproducible.
- Per-op budgets: each operation has its own deadline_ms.
- No ``symbol=`` labels anywhere.

This module has NO side-effects (no metrics, no I/O).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Stable reason strings (used as metric labels — do NOT rename)
# ---------------------------------------------------------------------------

REASON_TIMEOUT = "timeout"
REASON_CONNECT = "connect"
REASON_DNS = "dns"
REASON_TLS = "tls"
REASON_429 = "429"
REASON_5XX = "5xx"
REASON_4XX = "4xx"
REASON_DECODE = "decode"
REASON_UNKNOWN = "unknown"

# Reasons that are retryable by default for read operations
_RETRYABLE_READ: frozenset[str] = frozenset(
    {
        REASON_TIMEOUT,
        REASON_CONNECT,
        REASON_DNS,
        REASON_5XX,
        REASON_429,
    }
)

# Reasons that are retryable by default for write operations (more conservative)
_RETRYABLE_WRITE: frozenset[str] = frozenset(
    {
        REASON_TIMEOUT,
        REASON_CONNECT,
        REASON_DNS,
        REASON_5XX,
    }
)

# ---------------------------------------------------------------------------
# Ops taxonomy (SSOT — used as metric labels)
# ---------------------------------------------------------------------------

OP_PLACE_ORDER = "place_order"
OP_CANCEL_ORDER = "cancel_order"
OP_CANCEL_ALL = "cancel_all"
OP_GET_OPEN_ORDERS = "get_open_orders"
OP_GET_POSITIONS = "get_positions"
OP_GET_ACCOUNT = "get_account"
OP_EXCHANGE_INFO = "exchange_info"
OP_PING_TIME = "ping_time"
OP_GET_USER_TRADES = "get_user_trades"

WRITE_OPS: frozenset[str] = frozenset(
    {
        OP_PLACE_ORDER,
        OP_CANCEL_ORDER,
        OP_CANCEL_ALL,
    }
)

READ_OPS: frozenset[str] = frozenset(
    {
        OP_GET_OPEN_ORDERS,
        OP_GET_POSITIONS,
        OP_GET_ACCOUNT,
        OP_EXCHANGE_INFO,
        OP_PING_TIME,
        OP_GET_USER_TRADES,
    }
)

ALL_OPS: frozenset[str] = WRITE_OPS | READ_OPS


# ---------------------------------------------------------------------------
# HttpRetryPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRetryPolicy:
    """Configuration for HTTP retry behavior.

    Attributes:
        max_attempts: Maximum number of attempts (1 = no retries).
        base_delay_ms: Initial delay between retries in milliseconds.
        max_delay_ms: Maximum delay cap in milliseconds.
        backoff_multiplier: Multiplier for exponential backoff.
        jitter: Whether to add random jitter to delays.
                False by default for deterministic testing.
        retryable_reasons: Set of reasons that trigger retries.
    """

    max_attempts: int = 1
    base_delay_ms: int = 100
    max_delay_ms: int = 500
    backoff_multiplier: float = 2.0
    jitter: bool = False
    retryable_reasons: frozenset[str] = field(default_factory=lambda: _RETRYABLE_READ)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            msg = "max_attempts must be >= 1"
            raise ValueError(msg)
        if self.base_delay_ms < 0:
            msg = "base_delay_ms must be >= 0"
            raise ValueError(msg)
        if self.max_delay_ms < self.base_delay_ms:
            msg = "max_delay_ms must be >= base_delay_ms"
            raise ValueError(msg)

    def compute_delay_ms(self, attempt: int) -> int:
        """Compute delay for given attempt (0-indexed). Deterministic when jitter=False."""
        delay = self.base_delay_ms * (self.backoff_multiplier**attempt)
        delay = min(delay, self.max_delay_ms)
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return int(delay)

    @staticmethod
    def for_read(*, max_attempts: int = 1) -> HttpRetryPolicy:
        """Create a read-op policy (retries on timeout/connect/5xx/429)."""
        return HttpRetryPolicy(
            max_attempts=max_attempts,
            retryable_reasons=_RETRYABLE_READ,
        )

    @staticmethod
    def for_write(*, max_attempts: int = 1) -> HttpRetryPolicy:
        """Create a write-op policy (retries on timeout/connect/5xx only)."""
        return HttpRetryPolicy(
            max_attempts=max_attempts,
            retryable_reasons=_RETRYABLE_WRITE,
        )


# ---------------------------------------------------------------------------
# DeadlinePolicy (per-op budgets)
# ---------------------------------------------------------------------------

# Default deadlines (ms) — conservative starting values for Binance REST API.
# Tune after observing real p95/p99 from grinder_http_latency_ms in SHADOW/STAGING.
_DEFAULT_DEADLINES: dict[str, int] = {
    OP_PLACE_ORDER: 1500,
    OP_CANCEL_ORDER: 600,
    OP_CANCEL_ALL: 1200,  # heavier than single cancel; may need tuning with many open orders
    OP_GET_OPEN_ORDERS: 2000,
    OP_GET_POSITIONS: 2500,
    OP_GET_ACCOUNT: 2500,
    OP_EXCHANGE_INFO: 5000,
    OP_PING_TIME: 800,
    OP_GET_USER_TRADES: 2500,
}


@dataclass(frozen=True)
class DeadlinePolicy:
    """Per-operation deadline budgets.

    Attributes:
        deadlines: Map of op name to deadline in milliseconds.
    """

    deadlines: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_DEADLINES))

    def get_deadline_ms(self, op: str) -> int:
        """Get deadline for an operation. Falls back to 5000ms for unknown ops."""
        return self.deadlines.get(op, 5000)

    def get_deadline_s(self, op: str) -> float:
        """Get deadline as seconds (float)."""
        return self.get_deadline_ms(op) / 1000.0

    @staticmethod
    def defaults() -> DeadlinePolicy:
        """Create policy with default conservative deadlines."""
        return DeadlinePolicy()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_exception(error: Exception) -> str:
    """Classify an exception into a stable reason string."""
    error_type = type(error).__name__.lower()
    error_str = str(error).lower()

    if "timeout" in error_type or "timeout" in error_str:
        return REASON_TIMEOUT
    if "connect" in error_type or "connect" in error_str:
        return REASON_CONNECT
    if "dns" in error_str or "resolve" in error_str:
        return REASON_DNS
    if "ssl" in error_str or "tls" in error_str or "certificate" in error_str:
        return REASON_TLS
    if "decode" in error_str or "json" in error_str:
        return REASON_DECODE
    return REASON_UNKNOWN


def classify_http_error(
    *,
    status_code: int | None = None,
    error: Exception | None = None,
) -> str:
    """Classify an HTTP error into a stable reason string.

    Used for metrics labels and retry decisions. Returns one of the
    REASON_* constants.

    Args:
        status_code: HTTP status code (if available).
        error: Exception from httpx (if available).

    Returns:
        Stable reason string (e.g. "timeout", "5xx", "429").
    """
    if status_code is not None:
        if status_code == 429:
            return REASON_429
        if 500 <= status_code < 600:
            return REASON_5XX
        if 400 <= status_code < 500:
            return REASON_4XX

    if error is not None:
        return _classify_exception(error)

    return REASON_UNKNOWN


def is_http_retryable(reason: str, policy: HttpRetryPolicy) -> bool:
    """Check if a request with this failure reason should be retried.

    Args:
        reason: Error reason from classify_http_error().
        policy: Retry policy to check against.

    Returns:
        True if the reason is in the policy's retryable set.
    """
    return reason in policy.retryable_reasons
