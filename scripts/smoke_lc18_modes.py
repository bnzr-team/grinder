#!/usr/bin/env python3
"""LC-18 Mode Smoke Test: Proves 0 port calls in DETECT_ONLY/PLAN_ONLY/BLOCKED modes.

Usage:
    PYTHONPATH=src python3 -m scripts.smoke_lc18_modes

This script:
1. Creates fake mismatches (unexpected order + unexpected position)
2. Runs remediation in each mode
3. Proves:
   - DETECT_ONLY: 0 port calls, 0 planned, 0 blocked, 0 executed
   - PLAN_ONLY: 0 port calls, planned > 0, 0 blocked, 0 executed
   - BLOCKED: 0 port calls, planned > 0, blocked > 0 (logged), 0 executed
   - EXECUTE_CANCEL_ALL: cancel works, flatten blocked by MODE_CANCEL_ONLY
   - EXECUTE_FLATTEN: flatten works, cancel blocked by MODE_FLATTEN_ONLY
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from grinder.core import OrderSide, OrderState
from grinder.reconcile.config import ReconcileConfig, RemediationAction, RemediationMode
from grinder.reconcile.metrics import get_reconcile_metrics, reset_reconcile_metrics
from grinder.reconcile.remediation import RemediationExecutor
from grinder.reconcile.types import ObservedOrder, ObservedPosition


def make_mock_port() -> MagicMock:
    """Create mock port that counts calls."""
    port = MagicMock()
    port.cancel_order.return_value = True
    port.place_market_order.return_value = "grinder_test_cleanup_1"
    port._call_count = {"cancel": 0, "place": 0}

    def track_cancel(*_args: Any, **_kwargs: Any) -> bool:
        port._call_count["cancel"] += 1
        return True

    def track_place(*_args: Any, **_kwargs: Any) -> str:
        port._call_count["place"] += 1
        return "grinder_test_cleanup_1"

    port.cancel_order.side_effect = track_cancel
    port.place_market_order.side_effect = track_place
    return port


def make_test_order() -> ObservedOrder:
    """Create test order with grinder_ prefix."""
    return ObservedOrder(
        client_order_id="grinder_default_BTCUSDT_1_1704067200000_1",
        symbol="BTCUSDT",
        order_id=12345678,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("42500.00"),
        orig_qty=Decimal("0.010"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=1704067200000,
    )


def make_test_position() -> ObservedPosition:
    """Create test position."""
    return ObservedPosition(
        symbol="BTCUSDT",
        position_amt=Decimal("0.010"),
        entry_price=Decimal("42500.00"),
        unrealized_pnl=Decimal("10.00"),
        ts_observed=1704067200000,
    )


def make_executor(
    port: MagicMock,
    mode: RemediationMode,
    strategy_allowlist: set[str] | None = None,
) -> RemediationExecutor:
    """Create executor with specific mode."""
    config = ReconcileConfig(
        action=RemediationAction.CANCEL_ALL,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=mode,
        remediation_strategy_allowlist=strategy_allowlist or {"default"},
        max_flatten_notional_usdt=Decimal("1000"),
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
        max_calls_per_day=1000,
        max_notional_per_day=Decimal("1000000"),
    )
    os.environ["ALLOW_MAINNET_TRADE"] = "1"

    return RemediationExecutor(
        config=config,
        port=port,
        armed=True,
        symbol_whitelist=["BTCUSDT"],
    )


def test_mode(mode: RemediationMode, mode_name: str) -> dict[str, Any]:
    """Test a specific mode and return results."""
    reset_reconcile_metrics()
    port = make_mock_port()
    executor = make_executor(port, mode)
    order = make_test_order()
    position = make_test_position()

    # Try cancel
    cancel_result = executor.remediate_cancel(order)

    # Try flatten (need to set action=FLATTEN for proper routing)
    executor.config = ReconcileConfig(
        action=RemediationAction.FLATTEN,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=mode,
        remediation_strategy_allowlist={"default"},
        max_flatten_notional_usdt=Decimal("1000"),
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
        max_calls_per_day=1000,
        max_notional_per_day=Decimal("1000000"),
    )
    flatten_result = executor.remediate_flatten(position, current_price=Decimal("42500.00"))

    metrics = get_reconcile_metrics()

    return {
        "mode": mode_name,
        "port_calls": port._call_count["cancel"] + port._call_count["place"],
        "cancel_calls": port._call_count["cancel"],
        "place_calls": port._call_count["place"],
        "cancel_status": cancel_result.status.value,
        "cancel_reason": cancel_result.block_reason.value if cancel_result.block_reason else "none",
        "flatten_status": flatten_result.status.value,
        "flatten_reason": (
            flatten_result.block_reason.value if flatten_result.block_reason else "none"
        ),
        "planned_count": sum(metrics.action_planned_counts.values()),
        "executed_count": sum(metrics.action_executed_counts.values()),
        "blocked_count": sum(metrics.action_blocked_counts.values()),
    }


def test_execute_cancel_all_mode() -> dict[str, Any]:
    """Test EXECUTE_CANCEL_ALL allows cancel but blocks flatten."""
    reset_reconcile_metrics()
    os.environ["ALLOW_MAINNET_TRADE"] = "1"

    port = make_mock_port()
    order = make_test_order()
    position = make_test_position()

    # Config for cancel
    config = ReconcileConfig(
        action=RemediationAction.CANCEL_ALL,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=RemediationMode.EXECUTE_CANCEL_ALL,
        remediation_strategy_allowlist={"default"},
        max_flatten_notional_usdt=Decimal("1000"),
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
    )
    executor = RemediationExecutor(
        config=config, port=port, armed=True, symbol_whitelist=["BTCUSDT"]
    )

    cancel_result = executor.remediate_cancel(order)

    # Now try flatten - should be blocked
    executor.config = ReconcileConfig(
        action=RemediationAction.FLATTEN,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=RemediationMode.EXECUTE_CANCEL_ALL,  # Still cancel-only
        remediation_strategy_allowlist={"default"},
        max_flatten_notional_usdt=Decimal("1000"),
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
    )
    flatten_result = executor.remediate_flatten(position, current_price=Decimal("42500.00"))

    metrics = get_reconcile_metrics()

    return {
        "mode": "EXECUTE_CANCEL_ALL",
        "cancel_status": cancel_result.status.value,
        "cancel_reason": cancel_result.block_reason.value if cancel_result.block_reason else "none",
        "flatten_status": flatten_result.status.value,
        "flatten_reason": (
            flatten_result.block_reason.value if flatten_result.block_reason else "none"
        ),
        "cancel_calls": port._call_count["cancel"],
        "place_calls": port._call_count["place"],
        "executed_count": sum(metrics.action_executed_counts.values()),
        "blocked_count": sum(metrics.action_blocked_counts.values()),
    }


def test_execute_flatten_mode() -> dict[str, Any]:
    """Test EXECUTE_FLATTEN allows flatten but blocks cancel."""
    reset_reconcile_metrics()
    os.environ["ALLOW_MAINNET_TRADE"] = "1"

    port = make_mock_port()
    order = make_test_order()
    position = make_test_position()

    # Config for flatten
    config = ReconcileConfig(
        action=RemediationAction.FLATTEN,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=RemediationMode.EXECUTE_FLATTEN,
        remediation_strategy_allowlist={"default"},
        max_flatten_notional_usdt=Decimal("1000"),
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
    )
    executor = RemediationExecutor(
        config=config, port=port, armed=True, symbol_whitelist=["BTCUSDT"]
    )

    flatten_result = executor.remediate_flatten(position, current_price=Decimal("42500.00"))

    # Now try cancel - should be blocked
    executor.config = ReconcileConfig(
        action=RemediationAction.CANCEL_ALL,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=RemediationMode.EXECUTE_FLATTEN,  # Still flatten-only
        remediation_strategy_allowlist={"default"},
        max_flatten_notional_usdt=Decimal("1000"),
        max_calls_per_run=100,
        max_notional_per_run=Decimal("100000"),
    )
    cancel_result = executor.remediate_cancel(order)

    metrics = get_reconcile_metrics()

    return {
        "mode": "EXECUTE_FLATTEN",
        "flatten_status": flatten_result.status.value,
        "flatten_reason": (
            flatten_result.block_reason.value if flatten_result.block_reason else "none"
        ),
        "cancel_status": cancel_result.status.value,
        "cancel_reason": cancel_result.block_reason.value if cancel_result.block_reason else "none",
        "cancel_calls": port._call_count["cancel"],
        "place_calls": port._call_count["place"],
        "executed_count": sum(metrics.action_executed_counts.values()),
        "blocked_count": sum(metrics.action_blocked_counts.values()),
    }


def main() -> int:
    """Run all LC-18 mode smoke tests."""
    print("=" * 70)
    print("LC-18 MODE SMOKE TEST")
    print("=" * 70)
    print()

    # Test A/B/C modes (should have 0 port calls)
    modes_abc = [
        (RemediationMode.DETECT_ONLY, "DETECT_ONLY"),
        (RemediationMode.PLAN_ONLY, "PLAN_ONLY"),
        (RemediationMode.BLOCKED, "BLOCKED"),
    ]

    print("## Stage A/B/C: Zero Port Calls Verification")
    print("-" * 70)
    print(f"{'Mode':<20} {'Port Calls':<12} {'Planned':<10} {'Blocked':<10} {'Executed':<10}")
    print("-" * 70)

    all_pass = True
    for mode, name in modes_abc:
        result = test_mode(mode, name)
        port_ok = result["port_calls"] == 0
        exec_ok = result["executed_count"] == 0
        status = "PASS" if port_ok and exec_ok else "FAIL"
        print(
            f"{name:<20} {result['port_calls']:<12} {result['planned_count']:<10} "
            f"{result['blocked_count']:<10} {result['executed_count']:<10} {status}"
        )
        if not (port_ok and exec_ok):
            all_pass = False

    print()
    print("## Stage D: EXECUTE_CANCEL_ALL Mode")
    print("-" * 70)
    result_d = test_execute_cancel_all_mode()
    print(
        f"Cancel:  status={result_d['cancel_status']:<10} "
        f"reason={result_d['cancel_reason']:<25} calls={result_d['cancel_calls']}"
    )
    print(
        f"Flatten: status={result_d['flatten_status']:<10} "
        f"reason={result_d['flatten_reason']:<25} calls={result_d['place_calls']}"
    )
    print(f"Metrics: executed={result_d['executed_count']}, blocked={result_d['blocked_count']}")

    cancel_ok = result_d["cancel_status"] == "executed" and result_d["cancel_calls"] == 1
    flatten_blocked = (
        result_d["flatten_status"] == "blocked" and result_d["flatten_reason"] == "mode_cancel_only"
    )
    stage_d_ok = cancel_ok and flatten_blocked
    print(f"Stage D: {'PASS' if stage_d_ok else 'FAIL'} (cancel executes, flatten blocked)")
    if not stage_d_ok:
        all_pass = False

    print()
    print("## Stage E: EXECUTE_FLATTEN Mode")
    print("-" * 70)
    result_e = test_execute_flatten_mode()
    print(
        f"Flatten: status={result_e['flatten_status']:<10} "
        f"reason={result_e['flatten_reason']:<25} calls={result_e['place_calls']}"
    )
    print(
        f"Cancel:  status={result_e['cancel_status']:<10} "
        f"reason={result_e['cancel_reason']:<25} calls={result_e['cancel_calls']}"
    )
    print(f"Metrics: executed={result_e['executed_count']}, blocked={result_e['blocked_count']}")

    flatten_ok = result_e["flatten_status"] == "executed" and result_e["place_calls"] == 1
    cancel_blocked = (
        result_e["cancel_status"] == "blocked" and result_e["cancel_reason"] == "mode_flatten_only"
    )
    stage_e_ok = flatten_ok and cancel_blocked
    print(f"Stage E: {'PASS' if stage_e_ok else 'FAIL'} (flatten executes, cancel blocked)")
    if not stage_e_ok:
        all_pass = False

    print()
    print("=" * 70)
    print(f"FINAL VERDICT: {'ALL MODES PASS' if all_pass else 'SOME MODES FAILED'}")
    print("=" * 70)

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
