#!/usr/bin/env python3
"""Inject a fake mismatch and attempt remediation (LC-20 smoke test).

This script is designed to run inside Docker containers via `docker exec`.
It injects a fake unexpected order and attempts to cancel it via RemediationExecutor.

The result depends on HA role:
- ACTIVE (leader): Returns PLANNED (dry_run=True, armed=False, FakePort — never executes)
- STANDBY/UNKNOWN (follower): Returns BLOCKED with reason=not_leader

Usage (inside container):
    python /tmp/inject.py --role active
    python /tmp/inject.py --role standby
    python /tmp/inject.py --role unknown

Output (JSON):
    {"role": "active|standby|unknown", "status": "planned|blocked", "block_reason": "...", "metrics": {...}}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Add src to path if running from repo root
sys.path.insert(0, "/app/src")

from grinder.core import OrderSide, OrderState
from grinder.ha.role import HARole, set_ha_state
from grinder.reconcile.config import ReconcileConfig, RemediationAction, RemediationMode
from grinder.reconcile.identity import OrderIdentityConfig, generate_client_order_id
from grinder.reconcile.metrics import get_reconcile_metrics, reset_reconcile_metrics
from grinder.reconcile.remediation import RemediationExecutor
from grinder.reconcile.types import ObservedOrder


@dataclass
class FakePort:
    """Fake exchange port that records calls but makes no HTTP requests."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Record cancel call."""
        self.calls.append(
            {
                "method": "cancel_order",
                "symbol": symbol,
                "client_order_id": client_order_id,
                "ts": int(time.time() * 1000),
            }
        )
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "status": "CANCELED",
            "orderId": 123456789,
        }


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Inject mismatch and attempt remediation")
    parser.add_argument(
        "--role",
        type=str,
        choices=["active", "standby", "unknown"],
        required=True,
        help="HA role to simulate (active=leader, standby/unknown=follower)",
    )
    return parser.parse_args()


def main() -> int:
    """Run mismatch injection and remediation attempt."""
    # SAFETY GUARD: Fail if ALLOW_MAINNET_TRADE is set externally
    # This script uses FakePort and must NEVER execute real trades
    if os.getenv("ALLOW_MAINNET_TRADE") == "1":
        print(json.dumps({"error": "ALLOW_MAINNET_TRADE=1 is forbidden in smoke tests"}))
        return 2

    args = parse_args()

    # Reset metrics for clean state
    reset_reconcile_metrics()

    # Set HA state based on argument (since we're in a separate process)
    role_map = {
        "active": HARole.ACTIVE,
        "standby": HARole.STANDBY,
        "unknown": HARole.UNKNOWN,
    }
    role = role_map[args.role]
    set_ha_state(role=role)

    # Create identity config
    identity_config = OrderIdentityConfig(
        prefix="grinder_",
        strategy_id="smoke",
    )

    # Create reconcile config with all gates enabled EXCEPT dry_run
    # This allows us to see PLANNED on leader (dry_run=True) or EXECUTED (dry_run=False)
    config = ReconcileConfig(
        enabled=True,
        action=RemediationAction.CANCEL_ALL,
        remediation_mode=RemediationMode.EXECUTE_CANCEL_ALL,  # Full execution mode
        dry_run=True,  # But dry_run=True → PLANNED (not EXECUTED)
        allow_active_remediation=True,
        max_flatten_notional_usdt=Decimal("500"),
    )

    # Create fake port
    fake_port = FakePort()

    # Create executor with safety gates
    # NOTE: armed=False because this is a smoke test with FakePort
    # The test verifies HA role gating (not_leader) and dry_run behavior
    executor = RemediationExecutor(
        config=config,
        port=fake_port,  # type: ignore[arg-type]
        armed=False,  # Never arm in smoke tests
        symbol_whitelist=["BTCUSDT", "ETHUSDT"],
        identity_config=identity_config,
    )

    # Create a fake observed order with grinder_ prefix
    ts = int(time.time() * 1000)
    cid = generate_client_order_id(identity_config, "BTCUSDT", 1, ts, 0)
    observed_order = ObservedOrder(
        client_order_id=cid,
        symbol="BTCUSDT",
        order_id=123456789,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("50000.00"),
        orig_qty=Decimal("0.001"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=ts,
        source="smoke_test",
    )

    # Attempt remediation
    result = executor.remediate_cancel(observed_order)

    # Get metrics
    metrics = get_reconcile_metrics()

    # Build output
    output = {
        "role": role.value,
        "status": result.status.value,
        "block_reason": result.block_reason.value if result.block_reason else None,
        "metrics": {
            "action_planned_cancel_all": metrics.action_planned_counts.get("cancel_all", 0),
            "action_executed_cancel_all": metrics.action_executed_counts.get("cancel_all", 0),
            "action_blocked_not_leader": metrics.action_blocked_counts.get("not_leader", 0),
        },
        "port_calls": len(fake_port.calls),
    }

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
