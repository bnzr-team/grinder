"""Sync HTTP client wrapper with per-op deadlines, retries, and metrics (Launch-05 PR2).

Wraps any ``HttpClient`` (from binance_port.py) with:
- Per-op timeout from DeadlinePolicy (overrides ``timeout_ms``)
- Optional retries with exponential backoff
- Metrics recording via latency_metrics module

Design:
- When ``enabled=False`` (default): pure pass-through, byte-for-byte same behavior.
- When ``enabled=True``: applies per-op deadline, retries, records metrics.
- Injectable clock + sleep for deterministic testing.
- No ``symbol=`` labels anywhere.

This module imports from net.retry_policy (same package) and the HttpClient
Protocol from execution.binance_port. No imports from reconcile/.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from grinder.net.retry_policy import (
    DeadlinePolicy,
    HttpRetryPolicy,
    classify_http_error,
    is_http_retryable,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from grinder.execution.binance_port import HttpClient, HttpResponse
    from grinder.observability.latency_metrics import HttpMetrics

logger = logging.getLogger(__name__)


class MeasuredSyncHttpClient:
    """Sync HTTP client with per-op deadlines, retries, and metrics.

    Implements the ``HttpClient`` Protocol from binance_port.py.

    When ``enabled=False`` (default): delegates directly to inner client
    with no behavior change.

    When ``enabled=True`` and ``op`` is provided: uses DeadlinePolicy for
    per-op timeout, retries per HttpRetryPolicy, records metrics.

    Args:
        inner: Underlying HttpClient to wrap.
        deadline_policy: Per-op deadline budgets.
        retry_policy: Retry configuration (read or write).
        enabled: Whether deadlines/retries are active.
        clock: Clock function for timing (injectable for tests).
        sleep_func: Sleep function for retry delays (injectable for tests).
        metrics: HttpMetrics instance for recording.
    """

    def __init__(
        self,
        *,
        inner: HttpClient,
        deadline_policy: DeadlinePolicy | None = None,
        retry_policy: HttpRetryPolicy | None = None,
        enabled: bool = False,
        clock: Callable[[], float] | None = None,
        sleep_func: Callable[[float], None] | None = None,
        metrics: HttpMetrics | None = None,
    ) -> None:
        self._inner = inner
        self._deadline = deadline_policy or DeadlinePolicy.defaults()
        self._retry = retry_policy or HttpRetryPolicy()
        self._enabled = enabled
        self._clock = clock or time.monotonic
        self._sleep = sleep_func or time.sleep
        self._metrics = metrics

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",
    ) -> HttpResponse:
        """Execute HTTP request with optional deadlines and retries.

        When ``enabled=False`` or ``op`` is empty: delegates directly to
        inner client with original ``timeout_ms``.

        When ``enabled=True`` and ``op`` is provided: overrides timeout_ms
        with per-op deadline, retries on transient failures, records metrics.
        """
        if not self._enabled or not op:
            return self._inner.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                timeout_ms=timeout_ms,
                op=op,
            )

        return self._measured_request(method, url, params, headers, op)

    def _measured_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        op: str,
    ) -> HttpResponse:
        """Execute request with deadlines, retries, and metrics."""
        start = self._clock()
        deadline_ms = self._deadline.get_deadline_ms(op)
        max_attempts = self._retry.max_attempts

        for attempt in range(max_attempts):
            try:
                response = self._inner.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    timeout_ms=deadline_ms,
                    op=op,
                )
                elapsed_ms = int((self._clock() - start) * 1000)
                status_class = self._status_class(response.status_code)

                # Record metrics
                self._record_request(op, status_class)
                self._record_latency(op, float(elapsed_ms))

                # Non-2xx: classify and maybe retry
                if response.status_code >= 400:
                    reason = classify_http_error(status_code=response.status_code)
                    if is_http_retryable(reason, self._retry) and attempt + 1 < max_attempts:
                        self._record_retry(op, reason)
                        delay_ms = self._retry.compute_delay_ms(attempt)
                        self._sleep(delay_ms / 1000.0)
                        continue

                    self._record_fail(op, reason)
                    return response

                return response

            except Exception as exc:
                elapsed_ms = int((self._clock() - start) * 1000)
                reason = classify_http_error(error=exc)

                if is_http_retryable(reason, self._retry) and attempt + 1 < max_attempts:
                    self._record_retry(op, reason)
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
                    self._sleep(delay_ms / 1000.0)
                    continue

                self._record_fail(op, reason)
                self._record_latency(op, float(elapsed_ms))
                raise

        # Should not reach here, but satisfy type checker
        raise RuntimeError(f"HTTP {op} exhausted all {max_attempts} attempts")  # pragma: no cover

    def _record_request(self, op: str, status_class: str) -> None:
        if self._metrics is not None:
            self._metrics.record_request(op, status_class)

    def _record_retry(self, op: str, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.record_retry(op, reason)

    def _record_fail(self, op: str, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.record_fail(op, reason)

    def _record_latency(self, op: str, latency_ms: float) -> None:
        if self._metrics is not None:
            self._metrics.record_latency(op, latency_ms)

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
