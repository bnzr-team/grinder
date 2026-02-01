#!/usr/bin/env python3
"""
Run GRINDER in live mode.

Usage:
    python -m scripts.run_live --symbols BTCUSDT,ETHUSDT --metrics-port 9090
"""

import argparse
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from grinder.observability import build_healthz_body, build_metrics_body, set_start_time


class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health checks.

    Uses pure functions from grinder.observability.live_contract
    to build response bodies (testable without network).
    """

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/healthz":
            self._send_health()
        elif self.path == "/metrics":
            self._send_metrics()
        else:
            self.send_error(404)

    def _send_health(self) -> None:
        """Send health check response."""
        body = build_healthz_body()
        self.send_response(200)
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
    # Initialize start time for uptime tracking
    set_start_time(time.time())
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GRINDER live trading")
    parser.add_argument(
        "--symbols",
        type=str,
        default="BTCUSDT,ETHUSDT",
        help="Comma-separated symbols to trade",
    )
    parser.add_argument(
        "--duration-s",
        type=int,
        default=0,
        help="Duration in seconds (0 = run forever)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=9090,
        help="Port for metrics/health endpoint",
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]

    print("GRINDER starting...")
    print(f"  Symbols: {symbols}")
    print(f"  Metrics port: {args.metrics_port}")

    # Start health/metrics server
    server = run_server(args.metrics_port)
    print(f"  Health endpoint: http://localhost:{args.metrics_port}/healthz")

    # Handle graceful shutdown
    shutdown = threading.Event()

    def handle_signal(_sig: int, _frame: object) -> None:
        print("\nShutting down...")
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("\nGRINDER running. Press Ctrl+C to stop.")

    # Main loop
    start = time.time()
    while not shutdown.is_set():
        if args.duration_s > 0 and (time.time() - start) >= args.duration_s:
            print(f"\nDuration ({args.duration_s}s) reached.")
            break
        shutdown.wait(timeout=1.0)

    server.shutdown()
    print("GRINDER stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
