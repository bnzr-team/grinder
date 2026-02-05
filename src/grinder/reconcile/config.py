"""Configuration for reconciliation.

See ADR-042 for design decisions.
See ADR-043 for active remediation design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class RemediationAction(Enum):
    """Remediation action types.

    These values are STABLE and used in config files and metrics.
    DO NOT rename or remove values without migration.
    """

    NONE = "none"  # No action (default, passive mode)
    CANCEL_ALL = "cancel_all"  # Cancel unexpected grinder_ orders
    FLATTEN = "flatten"  # Close unexpected positions


@dataclass
class ReconcileConfig:
    """Configuration for ReconcileEngine.

    Passive Reconciliation (LC-09b):
        order_grace_period_ms: Grace period before ORDER_MISSING_ON_EXCHANGE (default 5000ms)
        snapshot_interval_sec: REST snapshot interval (default 60s)
        snapshot_retry_delay_sec: Retry delay on 429/5xx (default 5s)
        snapshot_max_retries: Max retries per snapshot (default 3)
        expected_max_orders: Max orders in expected state ring buffer (default 200)
        expected_ttl_ms: TTL for expected orders (default 24h)
        symbol_filter: Optional symbol to reconcile (None = all grinder_ orders)
        enabled: Whether reconciliation is enabled (default True)

    Active Remediation (LC-10):
        action: Remediation action type (default: NONE = passive only)
        dry_run: If True, plan but don't execute (default: True)
        allow_active_remediation: Second safety gate (default: False)
        max_orders_per_action: Max cancels per reconcile run (default: 10)
        max_symbols_per_action: Max symbols per reconcile run (default: 3)
        cooldown_seconds: Min time between real actions (default: 60)
        max_flatten_notional_usdt: Max position notional for flatten (default: 500)
        require_whitelist: Require non-empty symbol whitelist (default: True)
    """

    # Passive reconciliation (LC-09b)
    order_grace_period_ms: int = 5000  # 5 seconds
    snapshot_interval_sec: int = 60
    snapshot_retry_delay_sec: int = 5
    snapshot_max_retries: int = 3
    expected_max_orders: int = 200
    expected_ttl_ms: int = 86_400_000  # 24 hours
    symbol_filter: str | None = None
    enabled: bool = True

    # Active remediation (LC-10)
    action: RemediationAction = RemediationAction.NONE
    dry_run: bool = True  # Default: plan only, don't execute
    allow_active_remediation: bool = False  # Second safety gate
    max_orders_per_action: int = 10
    max_symbols_per_action: int = 3
    cooldown_seconds: int = 60
    max_flatten_notional_usdt: Decimal = field(default_factory=lambda: Decimal("500"))
    require_whitelist: bool = True
