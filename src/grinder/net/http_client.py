"""Thin HTTP client wrapper with per-op deadlines, retries, and metrics (Launch-05).

Wraps httpx.AsyncClient with:
- Per-op timeout from DeadlinePolicy
- Optional retries (disabled by default; enabled via ``LATENCY_RETRY_ENABLED=1``)
- Structured result with telemetry (attempts, elapsed_ms, reason)
- Metrics recording via latency_metrics module

Design:
- No behavior change by default (``enabled=False`` → pass-through).
- Injectable clock for deterministic testing.
- Injectable sleep for deterministic retry testing.
- No ``symbol=`` labels.

This module does NOT import from execution/ or reconcile/.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from grinder.net.retry_policy import (
    DeadlinePolicy,
    HttpRetryPolicy,
    classify_http_error,
    is_http_retryable,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpResult:
    """Structured result from an HTTP request.

    Attributes:
        status_code: HTTP status code (0 if request failed before response).
        json_data: Parsed JSON response body (empty dict on failure).
        ok: True if status_code is 2xx.
        attempts: Number of attempts made.
        elapsed_ms: Total elapsed time in milliseconds.
        reason: Error reason if failed (None on success).
    """

    status_code: int = 0
    json_data: dict[str, Any] | list[Any] = field(default_factory=dict)
    ok: bool = False
    attempts: int = 1
    elapsed_ms: int = 0
    reason: str | None = None


class HttpClientError(Exception):
    """Raised when HTTP request fails after all retries.

    Attributes:
        op: Operation name.
        reason: Classified error reason.
        attempts: Number of attempts made.
        elapsed_ms: Total elapsed time.
    """

    def __init__(
        self,
        op: str,
        reason: str,
        attempts: int,
        elapsed_ms: int,
        cause: Exception | None = None,
    ) -> None:
        self.op = op
        self.reason = reason
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        super().__init__(
            f"HTTP {op} failed: reason={reason} attempts={attempts} elapsed={elapsed_ms}ms"
        )
        if cause is not None:
            self.__cause__ = cause


class MeasuredHttpClient:
    """HTTP client with per-op deadlines, retries, and metrics.

    Usage::

        client = MeasuredHttpClient(
            inner=httpx.AsyncClient(base_url="https://api.binance.com"),
            deadline_policy=DeadlinePolicy.defaults(),
            retry_policy=HttpRetryPolicy.for_read(max_attempts=3),
            enabled=True,
        )
        result = await client.request("GET", "/api/v3/time", op="ping_time")

    When ``enabled=False`` (default), retries are skipped and deadlines
    are not enforced — the inner client's defaults are used. This is the
    safe-by-default behavior for PR1.
    """

    def __init__(
        self,
        *,
        inner: httpx.AsyncClient,
        deadline_policy: DeadlinePolicy | None = None,
        retry_policy: HttpRetryPolicy | None = None,
        enabled: bool = False,
        clock: Callable[[], float] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
        metrics_recorder: Callable[[str, str, int, int], None] | None = None,
    ) -> None:
        """Initialize measured HTTP client.

        Args:
            inner: httpx.AsyncClient to wrap.
            deadline_policy: Per-op deadline budgets.
            retry_policy: Retry configuration.
            enabled: Whether deadlines/retries are active.
            clock: Clock function for timing (injectable for tests).
            sleep_func: Sleep function for retry delays (injectable for tests).
            metrics_recorder: Callback(op, status_class, attempts, elapsed_ms)
                              for recording metrics. Injected to avoid circular imports.
        """
        self._inner = inner
        self._deadline = deadline_policy or DeadlinePolicy.defaults()
        self._retry = retry_policy or HttpRetryPolicy()
        self._enabled = enabled
        self._clock = clock or time.monotonic
        self._sleep = sleep_func or asyncio.sleep
        self._record = metrics_recorder

    async def request(
        self,
        method: str,
        url: str,
        *,
        op: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> HttpResult:
        """Execute HTTP request with optional deadlines and retries.

        Args:
            method: HTTP method (GET, POST, DELETE).
            url: URL path (relative to base_url on inner client).
            op: Operation name from ops taxonomy (e.g. "cancel_order").
            params: Query parameters.
            headers: HTTP headers.
            json_body: JSON request body.

        Returns:
            HttpResult with response data and telemetry.

        Raises:
            HttpClientError: If all retries exhausted or non-retryable error.
        """
        start = self._clock()
        timeout_s = self._deadline.get_deadline_s(op) if self._enabled else None
        max_attempts = self._retry.max_attempts if self._enabled else 1
        last_reason: str | None = None
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            try:
                response = await self._inner.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json_body,
                    timeout=timeout_s,
                )
                elapsed_ms = int((self._clock() - start) * 1000)
                status_class = self._status_class(response.status_code)

                # Record metrics
                if self._record is not None:
                    self._record(op, status_class, attempt + 1, elapsed_ms)

                # Non-2xx: classify and maybe retry
                if response.status_code >= 400:
                    reason = classify_http_error(status_code=response.status_code)
                    if (
                        self._enabled
                        and is_http_retryable(reason, self._retry)
                        and attempt + 1 < max_attempts
                    ):
                        last_reason = reason
                        delay_ms = self._retry.compute_delay_ms(attempt)
                        await self._sleep(delay_ms / 1000.0)
                        continue

                    # Not retryable or last attempt
                    try:
                        body = response.json()
                    except Exception:
                        body = {}
                    return HttpResult(
                        status_code=response.status_code,
                        json_data=body,
                        ok=False,
                        attempts=attempt + 1,
                        elapsed_ms=elapsed_ms,
                        reason=reason,
                    )

                # Success
                try:
                    body = response.json()
                except Exception:
                    body = {}
                return HttpResult(
                    status_code=response.status_code,
                    json_data=body,
                    ok=True,
                    attempts=attempt + 1,
                    elapsed_ms=elapsed_ms,
                )

            except Exception as exc:
                elapsed_ms = int((self._clock() - start) * 1000)
                reason = classify_http_error(error=exc)
                last_reason = reason
                last_exc = exc

                # Record failure metrics
                if self._record is not None:
                    self._record(op, reason, attempt + 1, elapsed_ms)

                if (
                    self._enabled
                    and is_http_retryable(reason, self._retry)
                    and attempt + 1 < max_attempts
                ):
                    delay_ms = self._retry.compute_delay_ms(attempt)
                    logger.debug(
                        "HTTP %s %s failed (attempt %d/%d, reason=%s), retrying in %dms",
                        op,
                        method,
                        attempt + 1,
                        max_attempts,
                        reason,
                        delay_ms,
                    )
                    await self._sleep(delay_ms / 1000.0)
                    continue

                # Not retryable or last attempt
                raise HttpClientError(
                    op=op,
                    reason=reason,
                    attempts=attempt + 1,
                    elapsed_ms=elapsed_ms,
                    cause=exc,
                ) from exc

        # Should not reach here, but satisfy type checker
        raise HttpClientError(  # pragma: no cover
            op=op,
            reason=last_reason or REASON_UNKNOWN,
            attempts=max_attempts,
            elapsed_ms=int((self._clock() - start) * 1000),
            cause=last_exc,
        )

    @staticmethod
    def _status_class(status_code: int) -> str:
        """Map status code to class string for metrics."""
        if 200 <= status_code < 300:
            return "2xx"
        if 300 <= status_code < 400:
            return "3xx"
        if 400 <= status_code < 500:
            return "4xx"
        if 500 <= status_code < 600:
            return "5xx"
        return "other"


# Re-export for convenience
REASON_UNKNOWN = "unknown"
