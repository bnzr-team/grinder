"""HTTP probe loop for observable latency/retry metrics (Launch-05c).

Periodically calls read-only public Binance endpoints through the same
MeasuredSyncHttpClient path used by run_live_reconcile.py, so that
grinder_http_* metrics are visible on /metrics in the same process.

Design:
- Uses only public endpoints (no API keys required).
- Only ops from the READ_OPS allowlist (no write ops ever).
- Deterministic: injectable sleep_func and rng for testing.
- Exceptions never crash the caller (caught and logged).

Env vars:
    HTTP_PROBE_ENABLED       "1" to enable (default: off)
    HTTP_PROBE_INTERVAL_MS   Probe interval in ms (default: 5000)
    HTTP_PROBE_JITTER_MS     Random jitter added to interval (default: 250)
    HTTP_PROBE_BASE_URL      Base URL for probe requests (default: https://fapi.binance.com)
    HTTP_PROBE_OPS           Comma-separated ops to probe (default: ping_time,exchange_info)
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading

from grinder.net.retry_policy import OP_EXCHANGE_INFO, OP_PING_TIME

logger = logging.getLogger(__name__)

# Public endpoint paths (no API key required)
_OP_PATHS: dict[str, str] = {
    OP_PING_TIME: "/fapi/v1/time",
    OP_EXCHANGE_INFO: "/fapi/v1/exchangeInfo",
}

# Only these ops are allowed for the probe (public, read-only)
PROBE_ALLOWED_OPS: frozenset[str] = frozenset(_OP_PATHS.keys())


class ProbeConfigError(Exception):
    """Invalid probe configuration."""


@dataclass(frozen=True)
class HttpProbeConfig:
    """Configuration for the HTTP probe loop."""

    enabled: bool = False
    interval_ms: int = 5000
    jitter_ms: int = 250
    base_url: str = "https://fapi.binance.com"
    ops: tuple[str, ...] = (OP_PING_TIME, OP_EXCHANGE_INFO)

    def __post_init__(self) -> None:
        for op in self.ops:
            if op not in PROBE_ALLOWED_OPS:
                allowed = ", ".join(sorted(PROBE_ALLOWED_OPS))
                raise ProbeConfigError(f"HTTP_PROBE_OPS: unknown op '{op}'. Allowed: {allowed}")

    @classmethod
    def from_env(cls) -> HttpProbeConfig:
        """Build config from environment variables."""
        enabled = os.environ.get("HTTP_PROBE_ENABLED", "") == "1"

        interval_ms = 5000
        raw_interval = os.environ.get("HTTP_PROBE_INTERVAL_MS", "").strip()
        if raw_interval:
            try:
                interval_ms = int(raw_interval)
            except ValueError:
                raise ProbeConfigError(
                    f"Invalid HTTP_PROBE_INTERVAL_MS='{raw_interval}'. Must be integer."
                ) from None

        jitter_ms = 250
        raw_jitter = os.environ.get("HTTP_PROBE_JITTER_MS", "").strip()
        if raw_jitter:
            try:
                jitter_ms = int(raw_jitter)
            except ValueError:
                raise ProbeConfigError(
                    f"Invalid HTTP_PROBE_JITTER_MS='{raw_jitter}'. Must be integer."
                ) from None

        base_url = os.environ.get("HTTP_PROBE_BASE_URL", "https://fapi.binance.com").rstrip("/")

        ops_str = os.environ.get("HTTP_PROBE_OPS", "ping_time,exchange_info").strip()
        ops = tuple(o.strip() for o in ops_str.split(",") if o.strip())

        return cls(
            enabled=enabled,
            interval_ms=interval_ms,
            jitter_ms=jitter_ms,
            base_url=base_url,
            ops=ops,
        )


@dataclass
class HttpProbeRunner:
    """Runs periodic HTTP probes through a measured client.

    Args:
        client: HttpClient (or MeasuredSyncHttpClient wrapper) to use.
        config: Probe configuration.
        rng: Injectable random for deterministic jitter (default: random.Random()).
    """

    client: Any  # HttpClient protocol
    config: HttpProbeConfig
    rng: random.Random = field(default_factory=random.Random)

    def run_once(self) -> dict[str, bool]:
        """Execute one probe cycle (one request per configured op).

        Returns:
            Dict mapping op name to success (True) or failure (False).
        """
        results: dict[str, bool] = {}
        for op in self.config.ops:
            path = _OP_PATHS[op]
            url = f"{self.config.base_url}{path}"
            try:
                self.client.request(
                    method="GET",
                    url=url,
                    timeout_ms=3000,
                    op=op,
                )
                results[op] = True
            except Exception:
                logger.warning("HTTP probe failed: op=%s url=%s", op, url, exc_info=True)
                results[op] = False
        return results

    def run_loop(self, stop_event: threading.Event) -> None:
        """Run probe loop until stop_event is set.

        Sleeps interval_ms + random jitter between cycles.
        Exceptions are caught per-cycle (never crashes).
        """
        logger.info(
            "HTTP probe loop started: ops=%s, interval=%dms, jitter=%dms, base_url=%s",
            self.config.ops,
            self.config.interval_ms,
            self.config.jitter_ms,
            self.config.base_url,
        )
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("HTTP probe cycle error", exc_info=True)

            jitter = self.rng.randint(0, max(0, self.config.jitter_ms))
            sleep_s = (self.config.interval_ms + jitter) / 1000.0
            stop_event.wait(timeout=sleep_s)

        logger.info("HTTP probe loop stopped")
