"""Configuration for reconciliation.

See ADR-042 for design decisions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReconcileConfig:
    """Configuration for ReconcileEngine.

    Attributes:
        order_grace_period_ms: Grace period before ORDER_MISSING_ON_EXCHANGE (default 5000ms)
        snapshot_interval_sec: REST snapshot interval (default 60s)
        snapshot_retry_delay_sec: Retry delay on 429/5xx (default 5s)
        snapshot_max_retries: Max retries per snapshot (default 3)
        expected_max_orders: Max orders in expected state ring buffer (default 200)
        expected_ttl_ms: TTL for expected orders (default 24h)
        symbol_filter: Optional symbol to reconcile (None = all grinder_ orders)
        enabled: Whether reconciliation is enabled (default True)
    """

    order_grace_period_ms: int = 5000  # 5 seconds
    snapshot_interval_sec: int = 60
    snapshot_retry_delay_sec: int = 5
    snapshot_max_retries: int = 3
    expected_max_orders: int = 200
    expected_ttl_ms: int = 86_400_000  # 24 hours
    symbol_filter: str | None = None
    enabled: bool = True
