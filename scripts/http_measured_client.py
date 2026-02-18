"""Shared factory for MeasuredSyncHttpClient (Launch-05c/05d).

Extracted from run_live_reconcile.py so both run_live.py (HTTP probe)
and run_live_reconcile.py (reconcile loop) use the exact same wiring.

Env vars (all optional, safe defaults):
    LATENCY_RETRY_ENABLED        "1" to enable per-op deadlines + retries (default: off)
    HTTP_MAX_ATTEMPTS_READ       Max attempts for read ops (default: 1)
    HTTP_MAX_ATTEMPTS_WRITE      Max attempts for write ops (default: 1)
    HTTP_DEADLINE_<OP>_MS        Per-op deadline override (e.g. HTTP_DEADLINE_CANCEL_ORDER_MS=400)

When disabled: returns MeasuredSyncHttpClient with enabled=False (pure pass-through).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.execution.binance_port import HttpResponse
from grinder.net.measured_sync import MeasuredSyncHttpClient
from grinder.net.retry_policy import DeadlinePolicy, HttpRetryPolicy
from grinder.observability.latency_metrics import get_http_metrics

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Configuration error from invalid environment variables."""


def _parse_int(name: str, value: str, default: int) -> int:
    """Parse integer env var with default."""
    if not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"Invalid {name}='{value}'. Must be integer.") from None


@dataclass
class RequestsHttpClient:
    """HTTP client using httpx for real API calls.

    Implements the HttpClient protocol (binance_port.py).
    Shared between run_live.py (probe) and run_live_reconcile.py (reconcile).
    Uses httpx (already a project dependency) instead of requests.
    """

    _client: httpx.Client = field(default_factory=httpx.Client, repr=False)

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5000,
        op: str = "",  # noqa: ARG002 — used by MeasuredSyncHttpClient wrapper
    ) -> HttpResponse:
        """Execute HTTP request via httpx."""
        timeout_s = timeout_ms / 1000.0

        try:
            resp = self._client.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=timeout_s,
            )
            return HttpResponse(
                status_code=resp.status_code,
                json_data=resp.json() if resp.content else {},
            )

        except httpx.TimeoutException as e:
            raise ConnectorTransientError(f"Request timeout: {e}") from e
        except httpx.ConnectError as e:
            raise ConnectorTransientError(f"Connection error: {e}") from e
        except httpx.HTTPError as e:
            raise ConnectorNonRetryableError(f"Request error: {e}") from e


def build_measured_client(inner: object) -> MeasuredSyncHttpClient:
    """Wrap inner HttpClient with MeasuredSyncHttpClient.

    When LATENCY_RETRY_ENABLED=1: applies per-op deadlines, retries, records metrics.
    When disabled (default): pure pass-through, zero behavior change.

    Args:
        inner: Any object conforming to the HttpClient protocol
               (has .request(method, url, params, headers, timeout_ms, op)).

    Returns:
        MeasuredSyncHttpClient wrapping inner.
    """
    enabled = os.environ.get("LATENCY_RETRY_ENABLED", "") == "1"

    max_read = _parse_int(
        "HTTP_MAX_ATTEMPTS_READ",
        os.environ.get("HTTP_MAX_ATTEMPTS_READ", ""),
        default=1,
    )
    max_write = _parse_int(
        "HTTP_MAX_ATTEMPTS_WRITE",
        os.environ.get("HTTP_MAX_ATTEMPTS_WRITE", ""),
        default=1,
    )

    # Build deadline overrides from HTTP_DEADLINE_<OP>_MS env vars
    deadline_overrides: dict[str, int] = {}
    for key, value in os.environ.items():
        if key.startswith("HTTP_DEADLINE_") and key.endswith("_MS"):
            op_name = key[len("HTTP_DEADLINE_") : -len("_MS")].lower()
            deadline_overrides[op_name] = _parse_int(key, value, default=0)

    deadline_policy = DeadlinePolicy.defaults()
    if deadline_overrides:
        merged = dict(deadline_policy.deadlines)
        merged.update(deadline_overrides)
        deadline_policy = DeadlinePolicy(deadlines=merged)

    # Use the more permissive read policy (covers both read+write ops at the protocol level).
    # The write-specific conservatism (e.g. no 429 retry) is handled by the retry_policy
    # at the MeasuredSyncHttpClient level, but since we use a single client instance
    # for all ops, we pick the read policy with the higher max_attempts.
    # Write ops naturally get fewer retries because they exclude 429 from retryable reasons.
    retry_policy = HttpRetryPolicy.for_read(max_attempts=max(max_read, max_write))

    if enabled:
        logger.info(
            "HTTP measured client ENABLED: max_read=%d, max_write=%d, deadline_overrides=%s",
            max_read,
            max_write,
            deadline_overrides or "none",
        )
    else:
        logger.info("HTTP measured client DISABLED (pass-through)")

    return MeasuredSyncHttpClient(
        inner=inner,  # type: ignore[arg-type]  # protocol-conforming object
        deadline_policy=deadline_policy,
        retry_policy=retry_policy,
        enabled=enabled,
        metrics=get_http_metrics(),
    )
