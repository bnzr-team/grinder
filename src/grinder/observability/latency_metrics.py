"""HTTP latency and retry metrics (Launch-05).

Provides Prometheus-format counters and histogram for HTTP operations:
- grinder_http_requests_total{op,status_class}
- grinder_http_retries_total{op,reason}
- grinder_http_fail_total{op,reason}
- grinder_http_latency_ms histogram {op,le} + _sum + _count

Labels use ``op`` and ``status_class``/``reason`` only.
No ``symbol=`` or other high-cardinality labels.

This module has NO imports from execution/ or reconcile/.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric names (stable contract — do not rename without updating metrics_contract.py)
METRIC_HTTP_REQUESTS = "grinder_http_requests_total"
METRIC_HTTP_RETRIES = "grinder_http_retries_total"
METRIC_HTTP_FAIL = "grinder_http_fail_total"
METRIC_HTTP_LATENCY = "grinder_http_latency_ms"

# Fixed histogram buckets (ms)
LATENCY_BUCKETS_MS: tuple[float, ...] = (
    50.0,
    100.0,
    200.0,
    400.0,
    800.0,
    1500.0,
    3000.0,
    5000.0,
    10000.0,
)


@dataclass
class HttpMetrics:
    """Prometheus counters and histogram for HTTP operations.

    Thread-safe via simple dict operations (GIL protection).
    """

    requests: dict[tuple[str, str], int] = field(default_factory=dict)
    retries: dict[tuple[str, str], int] = field(default_factory=dict)
    fails: dict[tuple[str, str], int] = field(default_factory=dict)
    latency_buckets: dict[str, dict[float, int]] = field(default_factory=dict)
    latency_sum: dict[str, float] = field(default_factory=dict)
    latency_count: dict[str, int] = field(default_factory=dict)

    def record_request(self, op: str, status_class: str) -> None:
        """Record an HTTP request completion."""
        key = (op, status_class)
        self.requests[key] = self.requests.get(key, 0) + 1

    def record_retry(self, op: str, reason: str) -> None:
        """Record an HTTP retry event."""
        key = (op, reason)
        self.retries[key] = self.retries.get(key, 0) + 1

    def record_fail(self, op: str, reason: str) -> None:
        """Record an HTTP request failure (all retries exhausted)."""
        key = (op, reason)
        self.fails[key] = self.fails.get(key, 0) + 1

    def record_latency(self, op: str, latency_ms: float) -> None:
        """Record an HTTP request latency observation."""
        if op not in self.latency_buckets:
            self.latency_buckets[op] = dict.fromkeys(LATENCY_BUCKETS_MS, 0)
            self.latency_sum[op] = 0.0
            self.latency_count[op] = 0

        for bucket in LATENCY_BUCKETS_MS:
            if latency_ms <= bucket:
                self.latency_buckets[op][bucket] += 1

        self.latency_sum[op] += latency_ms
        self.latency_count[op] += 1

    def _histogram_lines(self) -> list[str]:
        """Render latency histogram lines."""
        lines: list[str] = [
            f"# HELP {METRIC_HTTP_LATENCY} HTTP request latency in milliseconds",
            f"# TYPE {METRIC_HTTP_LATENCY} histogram",
        ]
        if self.latency_buckets:
            for op in sorted(self.latency_buckets):
                buckets = self.latency_buckets[op]
                total_count = self.latency_count.get(op, 0)
                total_sum = self.latency_sum.get(op, 0.0)

                for bucket in LATENCY_BUCKETS_MS:
                    bucket_count = buckets.get(bucket, 0)
                    lines.append(
                        f'{METRIC_HTTP_LATENCY}_bucket{{op="{op}",le="{bucket}"}} {bucket_count}'
                    )
                lines.append(f'{METRIC_HTTP_LATENCY}_bucket{{op="{op}",le="+Inf"}} {total_count}')
                lines.append(f'{METRIC_HTTP_LATENCY}_sum{{op="{op}"}} {total_sum}')
                lines.append(f'{METRIC_HTTP_LATENCY}_count{{op="{op}"}} {total_count}')
        else:
            lines.append(f'{METRIC_HTTP_LATENCY}_bucket{{op="none",le="+Inf"}} 0')
            lines.append(f'{METRIC_HTTP_LATENCY}_sum{{op="none"}} 0')
            lines.append(f'{METRIC_HTTP_LATENCY}_count{{op="none"}} 0')
        return lines

    def to_prometheus_lines(self) -> list[str]:
        """Render Prometheus text-format lines."""
        lines: list[str] = []

        # --- requests ---
        lines.append(f"# HELP {METRIC_HTTP_REQUESTS} Total HTTP requests by op and status class")
        lines.append(f"# TYPE {METRIC_HTTP_REQUESTS} counter")
        if self.requests:
            for (op, status_class), count in sorted(self.requests.items()):
                lines.append(
                    f'{METRIC_HTTP_REQUESTS}{{op="{op}",status_class="{status_class}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_HTTP_REQUESTS}{{op="none",status_class="none"}} 0')

        # --- retries ---
        lines.append(f"# HELP {METRIC_HTTP_RETRIES} Total HTTP retry events by op and reason")
        lines.append(f"# TYPE {METRIC_HTTP_RETRIES} counter")
        if self.retries:
            for (op, reason), count in sorted(self.retries.items()):
                lines.append(f'{METRIC_HTTP_RETRIES}{{op="{op}",reason="{reason}"}} {count}')
        else:
            lines.append(f'{METRIC_HTTP_RETRIES}{{op="none",reason="none"}} 0')

        # --- fails ---
        lines.append(f"# HELP {METRIC_HTTP_FAIL} Total HTTP failures by op and reason")
        lines.append(f"# TYPE {METRIC_HTTP_FAIL} counter")
        if self.fails:
            for (op, reason), count in sorted(self.fails.items()):
                lines.append(f'{METRIC_HTTP_FAIL}{{op="{op}",reason="{reason}"}} {count}')
        else:
            lines.append(f'{METRIC_HTTP_FAIL}{{op="none",reason="none"}} 0')

        # --- latency histogram ---
        lines.extend(self._histogram_lines())

        return lines

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        self.requests.clear()
        self.retries.clear()
        self.fails.clear()
        self.latency_buckets.clear()
        self.latency_sum.clear()
        self.latency_count.clear()


# Global singleton
_metrics: HttpMetrics | None = None


def get_http_metrics() -> HttpMetrics:
    """Get or create global HTTP metrics."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = HttpMetrics()
    return _metrics


def reset_http_metrics() -> None:
    """Reset HTTP metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
