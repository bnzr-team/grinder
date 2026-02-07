"""Integration tests for HA leader-only remediation (LC-20).

These tests verify RUNTIME behavior of HA role-based remediation gating:
- Leader (ACTIVE) can execute remediation
- Followers (STANDBY, UNKNOWN) are BLOCKED with reason=not_leader
- Blocked actions appear in action_blocked_total{reason="not_leader"} metric

This provides runtime proof that the HA gating works correctly.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from grinder.core import OrderSide, OrderState
from grinder.ha.role import HARole, reset_ha_state, set_ha_state
from grinder.reconcile.config import ReconcileConfig, RemediationAction, RemediationMode
from grinder.reconcile.metrics import (
    get_reconcile_metrics,
    reset_reconcile_metrics,
)
from grinder.reconcile.remediation import (
    RemediationBlockReason,
    RemediationExecutor,
    RemediationStatus,
)
from grinder.reconcile.types import ObservedOrder, ObservedPosition

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def clean_env() -> Generator[None, None, None]:
    """Clean environment variables and reset state."""
    # Save original state
    original_env = os.environ.get("ALLOW_MAINNET_TRADE")

    yield

    # Restore environment
    if original_env is None:
        os.environ.pop("ALLOW_MAINNET_TRADE", None)
    else:
        os.environ["ALLOW_MAINNET_TRADE"] = original_env

    # Reset HA state
    reset_ha_state()

    # Reset metrics
    reset_reconcile_metrics()


@pytest.fixture
def mock_port() -> MagicMock:
    """Create mock BinanceFuturesPort."""
    port = MagicMock()
    port.cancel_order.return_value = True
    port.place_market_order.return_value = "test_order_123"
    return port


@pytest.fixture
def observed_order() -> ObservedOrder:
    """Create test observed order with grinder_ prefix.

    Uses v1 format: {prefix}{strategy_id}_{symbol}_{level_id}_{ts}_{seq}
    """
    return ObservedOrder(
        client_order_id="grinder_default_BTCUSDT_L1_1234567890_0",
        symbol="BTCUSDT",
        order_id=12345,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=Decimal("42500.00"),
        orig_qty=Decimal("0.01"),
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=1000000,
        source="rest",
    )


@pytest.fixture
def observed_position() -> ObservedPosition:
    """Create test observed position."""
    return ObservedPosition(
        symbol="BTCUSDT",
        position_amt=Decimal("0.01"),
        entry_price=Decimal("42000.00"),
        unrealized_pnl=Decimal("5.00"),
        ts_observed=1000000,
        source="rest",
    )


def _make_fully_enabled_executor(port: MagicMock) -> RemediationExecutor:
    """Create executor with ALL gates enabled for execution."""
    config = ReconcileConfig(
        enabled=True,
        action=RemediationAction.CANCEL_ALL,
        dry_run=False,
        allow_active_remediation=True,
        remediation_mode=RemediationMode.EXECUTE_CANCEL_ALL,
        remediation_strategy_allowlist={"default"},
        remediation_symbol_allowlist={"BTCUSDT"},
        max_calls_per_day=100,
        max_notional_per_day=Decimal("10000"),
        max_calls_per_run=10,
        max_notional_per_run=Decimal("1000"),
        flatten_max_notional_per_call=Decimal("500"),
    )
    executor = RemediationExecutor(
        config=config,
        port=port,
        armed=True,
        symbol_whitelist=["BTCUSDT"],
        kill_switch_active=False,
    )
    return executor


class TestHALeaderOnlyIntegration:
    """Integration tests for LC-20 HA leader-only remediation.

    These tests verify the RUNTIME metrics behavior:
    - Leaders get EXECUTED status
    - Followers get BLOCKED status with reason=not_leader
    - action_blocked_total{reason="not_leader"} is incremented
    """

    def test_leader_executes_cancel(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Leader (ACTIVE) executes cancel and action_executed_total increments."""
        # Set HA role to ACTIVE (leader)
        set_ha_state(role=HARole.ACTIVE)
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor = _make_fully_enabled_executor(mock_port)

        # Execute remediation
        result = executor.remediate_cancel(observed_order)

        # Verify execution
        assert result.status == RemediationStatus.EXECUTED
        assert result.block_reason is None
        mock_port.cancel_order.assert_called_once()

        # Verify metrics
        metrics = get_reconcile_metrics()
        assert metrics.action_executed_counts.get("cancel_all", 0) == 1
        assert metrics.action_blocked_counts.get("not_leader", 0) == 0

        # Print for runtime proof
        print("\n=== LEADER CANCEL TEST ===")
        print(f"HA Role: {HARole.ACTIVE.value}")
        print(f"Result status: {result.status.value}")
        print(
            f"action_executed_total{{action='cancel_all'}}: {metrics.action_executed_counts.get('cancel_all', 0)}"
        )
        print(
            f"action_blocked_total{{reason='not_leader'}}: {metrics.action_blocked_counts.get('not_leader', 0)}"
        )

    def test_follower_blocked_cancel(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Follower (STANDBY) is BLOCKED and action_blocked_total{reason='not_leader'} increments."""
        # Set HA role to STANDBY (follower)
        set_ha_state(role=HARole.STANDBY)
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor = _make_fully_enabled_executor(mock_port)

        # Execute remediation
        result = executor.remediate_cancel(observed_order)

        # Verify BLOCKED (not PLANNED)
        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NOT_LEADER
        mock_port.cancel_order.assert_not_called()

        # Verify metrics - THIS IS THE KEY ASSERTION
        metrics = get_reconcile_metrics()
        assert metrics.action_blocked_counts.get("not_leader", 0) == 1
        assert metrics.action_executed_counts.get("cancel_all", 0) == 0
        assert metrics.action_planned_counts.get("cancel_all", 0) == 0

        # Print for runtime proof
        print("\n=== FOLLOWER CANCEL TEST ===")
        print(f"HA Role: {HARole.STANDBY.value}")
        print(f"Result status: {result.status.value}")
        print(f"Result block_reason: {result.block_reason.value}")
        print(
            f"action_blocked_total{{reason='not_leader'}}: {metrics.action_blocked_counts.get('not_leader', 0)}"
        )
        print(
            f"action_planned_total{{action='cancel_all'}}: {metrics.action_planned_counts.get('cancel_all', 0)}"
        )

    def test_unknown_role_blocked_cancel(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Unknown role is also BLOCKED with reason=not_leader."""
        # Set HA role to UNKNOWN (initial state, not yet elected)
        set_ha_state(role=HARole.UNKNOWN)
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor = _make_fully_enabled_executor(mock_port)

        # Execute remediation
        result = executor.remediate_cancel(observed_order)

        # Verify BLOCKED
        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NOT_LEADER
        mock_port.cancel_order.assert_not_called()

        # Verify metrics
        metrics = get_reconcile_metrics()
        assert metrics.action_blocked_counts.get("not_leader", 0) == 1

        # Print for runtime proof
        print("\n=== UNKNOWN ROLE TEST ===")
        print(f"HA Role: {HARole.UNKNOWN.value}")
        print(f"Result status: {result.status.value}")
        print(
            f"action_blocked_total{{reason='not_leader'}}: {metrics.action_blocked_counts.get('not_leader', 0)}"
        )

    def test_follower_blocked_flatten(
        self, mock_port: MagicMock, observed_position: ObservedPosition
    ) -> None:
        """Follower is BLOCKED for flatten with reason=not_leader."""
        # Set HA role to STANDBY (follower)
        set_ha_state(role=HARole.STANDBY)
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        # Create flatten-enabled executor
        config = ReconcileConfig(
            enabled=True,
            action=RemediationAction.FLATTEN,
            dry_run=False,
            allow_active_remediation=True,
            remediation_mode=RemediationMode.EXECUTE_FLATTEN,
            remediation_symbol_allowlist={"BTCUSDT"},
            max_calls_per_day=100,
            max_notional_per_day=Decimal("10000"),
            max_calls_per_run=10,
            max_notional_per_run=Decimal("1000"),
            flatten_max_notional_per_call=Decimal("500"),
        )
        executor = RemediationExecutor(
            config=config,
            port=mock_port,
            armed=True,
            symbol_whitelist=["BTCUSDT"],
            kill_switch_active=False,
        )

        # Execute remediation
        result = executor.remediate_flatten(observed_position, current_price=Decimal("42500.00"))

        # Verify BLOCKED
        assert result.status == RemediationStatus.BLOCKED
        assert result.block_reason == RemediationBlockReason.NOT_LEADER
        mock_port.place_market_order.assert_not_called()

        # Verify metrics
        metrics = get_reconcile_metrics()
        assert metrics.action_blocked_counts.get("not_leader", 0) == 1

        # Print for runtime proof
        print("\n=== FOLLOWER FLATTEN TEST ===")
        print(f"HA Role: {HARole.STANDBY.value}")
        print(f"Result status: {result.status.value}")
        print(
            f"action_blocked_total{{reason='not_leader'}}: {metrics.action_blocked_counts.get('not_leader', 0)}"
        )

    def test_multiple_follower_attempts_accumulate(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Multiple remediation attempts by follower accumulate in blocked counter."""
        # Set HA role to STANDBY (follower)
        set_ha_state(role=HARole.STANDBY)
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor = _make_fully_enabled_executor(mock_port)

        # Execute remediation 3 times
        for _ in range(3):
            result = executor.remediate_cancel(observed_order)
            assert result.status == RemediationStatus.BLOCKED
            assert result.block_reason == RemediationBlockReason.NOT_LEADER

        # Verify all 3 are counted
        metrics = get_reconcile_metrics()
        assert metrics.action_blocked_counts.get("not_leader", 0) == 3

        # Print for runtime proof
        print("\n=== MULTIPLE ATTEMPTS TEST ===")
        print(f"HA Role: {HARole.STANDBY.value}")
        print("Attempts: 3")
        print(
            f"action_blocked_total{{reason='not_leader'}}: {metrics.action_blocked_counts.get('not_leader', 0)}"
        )

    def test_prometheus_output_contains_not_leader(
        self, mock_port: MagicMock, observed_order: ObservedOrder
    ) -> None:
        """Prometheus /metrics output contains action_blocked_total{reason='not_leader'}."""
        # Set HA role to STANDBY (follower)
        set_ha_state(role=HARole.STANDBY)
        os.environ["ALLOW_MAINNET_TRADE"] = "1"

        executor = _make_fully_enabled_executor(mock_port)

        # Execute remediation
        result = executor.remediate_cancel(observed_order)
        assert result.status == RemediationStatus.BLOCKED

        # Get Prometheus output
        metrics = get_reconcile_metrics()
        prom_lines = metrics.to_prometheus_lines()
        prom_output = "\n".join(prom_lines)

        # Verify the metric is present
        assert 'grinder_reconcile_action_blocked_total{reason="not_leader"}' in prom_output

        # Parse the value
        for line in prom_lines:
            if 'grinder_reconcile_action_blocked_total{reason="not_leader"}' in line:
                value = line.split()[-1]
                assert int(value) >= 1
                break

        # Print for runtime proof
        print("\n=== PROMETHEUS OUTPUT TEST ===")
        print("Prometheus metrics output (filtered):")
        for line in prom_lines:
            if "action_blocked" in line or "action_planned" in line or "action_executed" in line:
                print(f"  {line}")
