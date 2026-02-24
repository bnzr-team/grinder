"""Execution port metrics for observability.

Metrics exported:
- grinder_port_order_attempts_total{port,op}: Counter of order operation attempts
- grinder_port_http_requests_total{port,method,route}: Counter of HTTP requests by port

These metric names and label keys are stable contracts.
DO NOT rename without updating metric contracts and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Metric name constants (stable contracts)
METRIC_PORT_ORDER_ATTEMPTS = "grinder_port_order_attempts_total"
METRIC_PORT_HTTP_REQUESTS = "grinder_port_http_requests_total"

# Label keys
LABEL_PORT = "port"
LABEL_OP = "op"
LABEL_METHOD = "method"
LABEL_ROUTE = "route"


@dataclass
class PortMetrics:
    """Execution port metrics collector.

    Tracks order operation attempts and HTTP requests by port type.
    Thread-safe via simple dict operations (GIL protection).
    """

    order_attempts: dict[tuple[str, str], int] = field(default_factory=dict)
    """Order attempt counter by (port_name, operation) tuple."""

    http_requests: dict[tuple[str, str, str], int] = field(default_factory=dict)
    """HTTP request counter by (port_name, method, route) tuple."""

    def record_order_attempt(self, port: str, op: str) -> None:
        """Record an order operation attempt.

        Args:
            port: Port name (e.g., "futures", "noop")
            op: Operation name (e.g., "place", "cancel", "replace")
        """
        key = (port, op)
        self.order_attempts[key] = self.order_attempts.get(key, 0) + 1

    def record_http_request(self, port: str, method: str, route: str) -> None:
        """Record an HTTP request attempt.

        Args:
            port: Port name (e.g., "futures")
            method: HTTP method (e.g., "GET", "POST", "DELETE")
            route: URL path without query string (e.g., "/fapi/v1/order")
        """
        key = (port, method.upper(), route)
        self.http_requests[key] = self.http_requests.get(key, 0) + 1

    def initialize_zero_series(self, port: str) -> None:
        """Pre-populate zero-value series for a port.

        Ensures Prometheus scrapes show 0-value series immediately.
        Idempotent: does not reset already-incremented counters.

        Args:
            port: Port name to initialize (e.g., "futures", "noop")
        """
        for op in ("place", "cancel", "replace"):
            key = (port, op)
            if key not in self.order_attempts:
                self.order_attempts[key] = 0

    def to_prometheus_lines(self) -> list[str]:
        """Export metrics in Prometheus text format."""
        lines: list[str] = []

        # --- order attempts ---
        lines.append(
            f"# HELP {METRIC_PORT_ORDER_ATTEMPTS} Total order operation attempts by port and operation"
        )
        lines.append(f"# TYPE {METRIC_PORT_ORDER_ATTEMPTS} counter")

        if self.order_attempts:
            for (port, op), count in sorted(self.order_attempts.items()):
                lines.append(
                    f'{METRIC_PORT_ORDER_ATTEMPTS}{{{LABEL_PORT}="{port}",{LABEL_OP}="{op}"}} {count}'
                )
        else:
            lines.append(f'{METRIC_PORT_ORDER_ATTEMPTS}{{{LABEL_PORT}="none",{LABEL_OP}="none"}} 0')

        # --- HTTP requests ---
        lines.append(
            f"# HELP {METRIC_PORT_HTTP_REQUESTS} Total HTTP requests by port, method, and route"
        )
        lines.append(f"# TYPE {METRIC_PORT_HTTP_REQUESTS} counter")

        if self.http_requests:
            for (port, method, route), count in sorted(self.http_requests.items()):
                lines.append(
                    f"{METRIC_PORT_HTTP_REQUESTS}"
                    f'{{{LABEL_PORT}="{port}",{LABEL_METHOD}="{method}",{LABEL_ROUTE}="{route}"}}'
                    f" {count}"
                )
        else:
            lines.append(
                f"{METRIC_PORT_HTTP_REQUESTS}"
                f'{{{LABEL_PORT}="none",{LABEL_METHOD}="none",{LABEL_ROUTE}="none"}}'
                " 0"
            )

        return lines

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        self.order_attempts.clear()
        self.http_requests.clear()


# Global singleton
_metrics: PortMetrics | None = None


def get_port_metrics() -> PortMetrics:
    """Get or create global port metrics instance."""
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = PortMetrics()
    return _metrics


def reset_port_metrics() -> None:
    """Reset port metrics (for testing)."""
    global _metrics  # noqa: PLW0603
    _metrics = None
