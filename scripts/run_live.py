#!/usr/bin/env python3
"""
Run GRINDER in live mode with HA support.

Usage:
    python -m scripts.run_live --symbols BTCUSDT,ETHUSDT --metrics-port 9090

HA Mode (enabled via GRINDER_HA_ENABLED=true):
    - Starts LeaderElector for single-active coordination
    - /readyz returns 200 only when ACTIVE (ready to trade)
    - Automatic failover on lock loss (fail-safe to STANDBY)

HTTP Probe (enabled via HTTP_PROBE_ENABLED=1, Launch-05c):
    - Periodically calls public Binance endpoints through MeasuredSyncHttpClient
    - Generates grinder_http_* metrics observable on /metrics
    - No API keys required (uses public endpoints only)
    - Requires LATENCY_RETRY_ENABLED=1 for metrics to be recorded
"""

import argparse
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from grinder.ha.leader import LeaderElector, LeaderElectorConfig
from grinder.ha.role import get_ha_state
from grinder.observability import (
    build_healthz_body,
    build_metrics_body,
    build_readyz_body,
    set_start_time,
)


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and metrics.

    Endpoints:
        /healthz - Always 200 if process alive (liveness)
        /readyz  - 200 if ACTIVE, 503 otherwise (readiness)
        /metrics - Prometheus metrics
    """

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/healthz":
            self._send_health()
        elif self.path == "/readyz":
            self._send_ready()
        elif self.path == "/metrics":
            self._send_metrics()
        else:
            self.send_error(404)

    def _send_health(self) -> None:
        """Send health check response (always 200 if alive)."""
        body = build_healthz_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_ready(self) -> None:
        """Send readiness check response (200 if ACTIVE, 503 otherwise)."""
        body, is_ready = build_readyz_body()
        status = 200 if is_ready else 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_metrics(self) -> None:
        """Send Prometheus metrics."""
        body = build_metrics_body()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default logging."""
        pass


def run_server(port: int) -> HTTPServer:
    """Start HTTP server in background thread."""
    set_start_time(time.time())
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def is_ha_enabled() -> bool:
    """Check if HA mode is enabled via environment."""
    return os.environ.get("GRINDER_HA_ENABLED", "").lower() in ("true", "1", "yes")


def start_ha_elector() -> LeaderElector | None:
    """Start HA leader election if enabled.

    Returns:
        LeaderElector instance if started, None otherwise.
    """
    if not is_ha_enabled():
        print("  HA mode: DISABLED (set GRINDER_HA_ENABLED=true to enable)")
        return None

    print("  HA mode: ENABLED")
    try:
        config = LeaderElectorConfig()
        print(f"    Redis URL: {config.redis_url}")
        print(f"    Lock TTL: {config.lock_ttl_ms}ms")
        print(f"    Instance ID: {config.instance_id}")
        elector = LeaderElector(config)
        elector.start()
        print("    LeaderElector started")
        return elector
    except Exception as e:
        print(f"    WARNING: Failed to start LeaderElector: {e}")
        print("    Running without HA (role will remain UNKNOWN)")
        return None


def start_http_probe(shutdown: threading.Event) -> threading.Thread | None:
    """Start HTTP probe loop if HTTP_PROBE_ENABLED=1 (Launch-05c).

    Returns:
        Background thread running the probe, or None if disabled.
    """
    from scripts.http_measured_client import (  # noqa: PLC0415
        RequestsHttpClient,
        build_measured_client,
    )
    from scripts.http_probe import HttpProbeConfig, HttpProbeRunner  # noqa: PLC0415

    config = HttpProbeConfig.from_env()
    if not config.enabled:
        print("  HTTP probe: DISABLED (set HTTP_PROBE_ENABLED=1 to enable)")
        return None

    inner = RequestsHttpClient()
    client = build_measured_client(inner)
    runner = HttpProbeRunner(client=client, config=config)

    print(f"  HTTP probe: ENABLED (ops={config.ops}, interval={config.interval_ms}ms)")

    thread = threading.Thread(
        target=runner.run_loop,
        args=(shutdown,),
        daemon=True,
        name="http-probe",
    )
    thread.start()
    return thread


def run_main_loop(shutdown: threading.Event, duration_s: int) -> None:
    """Run main event loop, logging role changes."""
    state = get_ha_state()
    print(f"  Current HA role: {state.role.value}")

    start = time.time()
    last_role = state.role
    while not shutdown.is_set():
        if duration_s > 0 and (time.time() - start) >= duration_s:
            print(f"\nDuration ({duration_s}s) reached.")
            break
        current_state = get_ha_state()
        if current_state.role != last_role:
            print(f"  HA role changed: {last_role.value} -> {current_state.role.value}")
            last_role = current_state.role
        shutdown.wait(timeout=1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GRINDER live trading")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT")
    parser.add_argument("--duration-s", type=int, default=0)
    parser.add_argument("--metrics-port", type=int, default=9090)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    print("GRINDER starting...")
    print(f"  Symbols: {symbols}")
    print(f"  Metrics port: {args.metrics_port}")

    server = run_server(args.metrics_port)
    print(f"  Health endpoint: http://localhost:{args.metrics_port}/healthz")
    print(f"  Ready endpoint:  http://localhost:{args.metrics_port}/readyz")

    elector = start_ha_elector()

    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())

    probe_thread = start_http_probe(shutdown)

    print("\nGRINDER running. Press Ctrl+C to stop.")
    run_main_loop(shutdown, args.duration_s)

    if elector is not None:
        print("  Stopping LeaderElector...")
        elector.stop()
    if probe_thread is not None:
        print("  Stopping HTTP probe...")
        probe_thread.join(timeout=5.0)
    server.shutdown()
    print("GRINDER stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
