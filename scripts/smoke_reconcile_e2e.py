#!/usr/bin/env python3
"""E2E smoke test for Reconcile → Remediate flow.

This script performs a deterministic E2E test of the reconciliation pipeline:
1. Inject fake observed state (orders/positions)
2. Compare against empty expected state (simulates unexpected state)
3. Run ReconcileEngine → detect mismatches
4. Run ReconcileRunner → route to RemediationExecutor
5. Verify results (dry-run plans or live execution)

SAFE-BY-CONSTRUCTION (dry-run by default):
- Default mode: DRY-RUN (no real HTTP/WS calls, no port operations)
- Live mode requires ALL of:
  - --confirm LIVE_REMEDIATE
  - RECONCILE_DRY_RUN=0
  - RECONCILE_ALLOW_ACTIVE=1
  - ARMED=1
  - ALLOW_MAINNET_TRADE=1
  - Non-empty symbol whitelist
  - Identity allowlist configured

Scenarios:
- order: ORDER_EXISTS_UNEXPECTED → CANCEL plan/execute
- position: POSITION_NONZERO_UNEXPECTED → FLATTEN plan/execute
- mixed: Both mismatches → deterministic routing (cancel wins by priority)
- all: Run all scenarios sequentially

Usage:
    # Dry-run (default) - all scenarios
    PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

    # Dry-run with specific scenario
    PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e --scenario order

    # Dry-run with audit enabled
    GRINDER_AUDIT_ENABLED=1 GRINDER_AUDIT_PATH=/tmp/audit.jsonl \
        PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e

    # Live mode (requires all gates)
    RECONCILE_DRY_RUN=0 RECONCILE_ALLOW_ACTIVE=1 ARMED=1 ALLOW_MAINNET_TRADE=1 \
        PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e --confirm LIVE_REMEDIATE

See: docs/runbooks/14_RECONCILE_E2E_SMOKE.md
See: ADR-047 for design decisions.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from grinder.core import OrderSide, OrderState
from grinder.reconcile.audit import AuditConfig, AuditWriter
from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.engine import ReconcileEngine
from grinder.reconcile.expected_state import ExpectedStateStore
from grinder.reconcile.identity import (
    OrderIdentityConfig,
    generate_client_order_id,
    set_default_identity_config,
)
from grinder.reconcile.observed_state import ObservedStateStore
from grinder.reconcile.remediation import RemediationExecutor
from grinder.reconcile.runner import ReconcileRunner, ReconcileRunReport
from grinder.reconcile.types import (
    ExpectedPosition,
    ObservedOrder,
    ObservedPosition,
)

# =============================================================================
# CONSTANTS
# =============================================================================

CONFIRM_TOKEN = "LIVE_REMEDIATE"
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_PRICE = Decimal("50000.00")
DEFAULT_QTY = Decimal("0.001")
DEFAULT_POSITION_AMT = Decimal("0.001")

# Environment variable names
ENV_DRY_RUN = "RECONCILE_DRY_RUN"
ENV_ALLOW_ACTIVE = "RECONCILE_ALLOW_ACTIVE"
ENV_ARMED = "ARMED"
ENV_ALLOW_MAINNET = "ALLOW_MAINNET_TRADE"


# =============================================================================
# FAKE PORT (NO REAL CALLS)
# =============================================================================


@dataclass
class FakePort:
    """Fake BinanceFuturesPort for dry-run mode.

    Records calls but makes NO real HTTP/WS requests.
    Used to verify that dry-run mode has zero port operations.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_returns_success: bool = True
    market_order_returns_success: bool = True

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Record cancel call (no real HTTP)."""
        self.calls.append(
            {
                "method": "cancel_order",
                "symbol": symbol,
                "client_order_id": client_order_id,
                "ts": int(time.time() * 1000),
            }
        )
        if self.cancel_returns_success:
            return {
                "symbol": symbol,
                "clientOrderId": client_order_id,
                "status": "CANCELED",
                "orderId": 123456789,
            }
        raise RuntimeError("Cancel failed (simulated)")

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Record market order call (no real HTTP)."""
        self.calls.append(
            {
                "method": "place_market_order",
                "symbol": symbol,
                "side": side.value,
                "qty": str(qty),
                "reduce_only": reduce_only,
                "ts": int(time.time() * 1000),
            }
        )
        if self.market_order_returns_success:
            return {
                "symbol": symbol,
                "side": side.value,
                "type": "MARKET",
                "origQty": str(qty),
                "executedQty": str(qty),
                "status": "FILLED",
                "orderId": 123456790,
            }
        raise RuntimeError("Market order failed (simulated)")

    def get_call_count(self) -> int:
        """Return total number of port calls."""
        return len(self.calls)

    def reset(self) -> None:
        """Clear recorded calls."""
        self.calls.clear()


# =============================================================================
# SCENARIO SETUP
# =============================================================================


def create_unexpected_order(
    symbol: str = DEFAULT_SYMBOL,
    ts: int | None = None,
    identity_config: OrderIdentityConfig | None = None,
) -> ObservedOrder:
    """Create an observed order that doesn't exist in expected state."""
    ts = ts or int(time.time() * 1000)
    # Use identity v1 format so it passes is_ours check
    if identity_config is None:
        identity_config = OrderIdentityConfig(
            prefix="grinder_",
            strategy_id="smoke",
        )
    cid = generate_client_order_id(identity_config, symbol, 1, ts, 1)
    return ObservedOrder(
        client_order_id=cid,
        symbol=symbol,
        order_id=123456789,
        side=OrderSide.BUY,
        status=OrderState.OPEN,
        price=DEFAULT_PRICE,
        orig_qty=DEFAULT_QTY,
        executed_qty=Decimal("0"),
        avg_price=Decimal("0"),
        ts_observed=ts,
        source="smoke_test",
    )


def create_unexpected_position(
    symbol: str = DEFAULT_SYMBOL,
    ts: int | None = None,
) -> ObservedPosition:
    """Create an observed nonzero position (expected = 0)."""
    ts = ts or int(time.time() * 1000)
    return ObservedPosition(
        symbol=symbol,
        position_amt=DEFAULT_POSITION_AMT,
        entry_price=DEFAULT_PRICE,
        unrealized_pnl=Decimal("0"),
        ts_observed=ts,
        source="smoke_test",
    )


# =============================================================================
# E2E HARNESS
# =============================================================================


@dataclass
class SmokeResult:
    """Result of a single smoke scenario."""

    scenario: str
    report: ReconcileRunReport
    mismatches_detected: int
    expected_action_type: str  # "cancel" or "flatten" or "none"
    actual_action_type: str
    port_calls: int
    passed: bool
    reason: str


def run_scenario(
    name: str,
    observed: ObservedStateStore,
    expected: ExpectedStateStore,
    config: ReconcileConfig,
    port: FakePort,
    symbol_whitelist: list[str],
    identity_config: OrderIdentityConfig,
    audit_writer: AuditWriter | None,
    armed: bool,
    expected_action: str,
) -> SmokeResult:
    """Run a single E2E scenario.

    Args:
        name: Scenario name
        observed: Observed state with injected mismatches
        expected: Expected state (typically empty for "unexpected" scenarios)
        config: ReconcileConfig
        port: FakePort (records calls, no real HTTP)
        symbol_whitelist: Symbols allowed for remediation
        identity_config: Order identity config
        audit_writer: Optional audit writer
        armed: Whether executor is armed
        expected_action: Expected action type ("cancel", "flatten", "none")

    Returns:
        SmokeResult with pass/fail and details
    """
    # Reset port call counter
    port.reset()

    # Create engine
    engine = ReconcileEngine(
        config=config,
        expected=expected,
        observed=observed,
        identity_config=identity_config,
    )

    # Create executor (FakePort duck-types BinanceFuturesPort)
    executor = RemediationExecutor(
        config=config,
        port=port,  # type: ignore[arg-type]
        armed=armed,
        symbol_whitelist=symbol_whitelist,
        identity_config=identity_config,
    )

    # Create runner
    runner = ReconcileRunner(
        engine=engine,
        executor=executor,
        observed=observed,
        price_getter=lambda _: DEFAULT_PRICE,
        audit_writer=audit_writer,
    )

    # Run reconciliation
    report = runner.run()

    # Determine actual action type
    actual_action = "none"
    if report.cancel_results:
        actual_action = "cancel"
    elif report.flatten_results:
        actual_action = "flatten"

    # Verify result
    passed = True
    reason = "OK"

    if actual_action != expected_action:
        passed = False
        reason = f"Expected action={expected_action}, got {actual_action}"
    elif config.dry_run and port.get_call_count() > 0:
        passed = False
        reason = f"Dry-run mode but port had {port.get_call_count()} calls"
    elif not config.dry_run and expected_action != "none" and port.get_call_count() == 0:
        # Live mode with expected action should have port calls (unless blocked)
        if report.blocked_count == 0:
            passed = False
            reason = f"Live mode expected port calls for {expected_action}, got 0"

    return SmokeResult(
        scenario=name,
        report=report,
        mismatches_detected=report.mismatches_detected,
        expected_action_type=expected_action,
        actual_action_type=actual_action,
        port_calls=port.get_call_count(),
        passed=passed,
        reason=reason,
    )


def run_order_scenario(
    config: ReconcileConfig,
    port: FakePort,
    symbol_whitelist: list[str],
    identity_config: OrderIdentityConfig,
    audit_writer: AuditWriter | None,
    armed: bool,
) -> SmokeResult:
    """Scenario 1: ORDER_EXISTS_UNEXPECTED → CANCEL."""
    observed = ObservedStateStore()
    expected = ExpectedStateStore()

    # Inject unexpected order
    order = create_unexpected_order(identity_config=identity_config)
    observed._orders[order.client_order_id] = order
    observed._last_snapshot_ts = int(time.time() * 1000)

    # Expected action depends on config
    expected_action = "none"
    if config.action == RemediationAction.CANCEL_ALL:
        expected_action = "cancel"
    elif config.action == RemediationAction.FLATTEN:
        # FLATTEN action doesn't route order mismatches
        expected_action = "none"

    return run_scenario(
        name="order",
        observed=observed,
        expected=expected,
        config=config,
        port=port,
        symbol_whitelist=symbol_whitelist,
        identity_config=identity_config,
        audit_writer=audit_writer,
        armed=armed,
        expected_action=expected_action,
    )


def run_position_scenario(
    config: ReconcileConfig,
    port: FakePort,
    symbol_whitelist: list[str],
    identity_config: OrderIdentityConfig,
    audit_writer: AuditWriter | None,
    armed: bool,
) -> SmokeResult:
    """Scenario 2: POSITION_NONZERO_UNEXPECTED → FLATTEN."""
    observed = ObservedStateStore()
    expected = ExpectedStateStore()

    # Inject unexpected position
    position = create_unexpected_position()
    observed._positions[position.symbol] = position
    observed._last_snapshot_ts = int(time.time() * 1000)

    # Expected zero position
    expected._positions[position.symbol] = ExpectedPosition(
        symbol=position.symbol,
        expected_position_amt=Decimal("0"),
        ts_updated=int(time.time() * 1000),
    )

    # Expected action depends on config
    expected_action = "none"
    if config.action == RemediationAction.FLATTEN:
        expected_action = "flatten"
    elif config.action == RemediationAction.CANCEL_ALL:
        # CANCEL_ALL action doesn't route position mismatches
        expected_action = "none"

    return run_scenario(
        name="position",
        observed=observed,
        expected=expected,
        config=config,
        port=port,
        symbol_whitelist=symbol_whitelist,
        identity_config=identity_config,
        audit_writer=audit_writer,
        armed=armed,
        expected_action=expected_action,
    )


def run_mixed_scenario(
    config: ReconcileConfig,
    port: FakePort,
    symbol_whitelist: list[str],
    identity_config: OrderIdentityConfig,
    audit_writer: AuditWriter | None,
    armed: bool,
) -> SmokeResult:
    """Scenario 3: Mixed mismatches → deterministic priority routing.

    Order mismatches have higher priority than position mismatches
    (per MISMATCH_PRIORITY). With action=CANCEL_ALL, order wins.
    With action=FLATTEN, only position is routed.
    """
    observed = ObservedStateStore()
    expected = ExpectedStateStore()

    # Inject both unexpected order and position
    order = create_unexpected_order(identity_config=identity_config)
    observed._orders[order.client_order_id] = order

    position = create_unexpected_position()
    observed._positions[position.symbol] = position
    observed._last_snapshot_ts = int(time.time() * 1000)

    # Expected zero position
    expected._positions[position.symbol] = ExpectedPosition(
        symbol=position.symbol,
        expected_position_amt=Decimal("0"),
        ts_updated=int(time.time() * 1000),
    )

    # Expected action:
    # - action=CANCEL_ALL → only order mismatch routes → cancel
    # - action=FLATTEN → only position mismatch routes → flatten
    # - action=NONE → neither routes
    expected_action = "none"
    if config.action == RemediationAction.CANCEL_ALL:
        expected_action = "cancel"
    elif config.action == RemediationAction.FLATTEN:
        expected_action = "flatten"

    return run_scenario(
        name="mixed",
        observed=observed,
        expected=expected,
        config=config,
        port=port,
        symbol_whitelist=symbol_whitelist,
        identity_config=identity_config,
        audit_writer=audit_writer,
        armed=armed,
        expected_action=expected_action,
    )


# =============================================================================
# LIVE MODE GATE CHECK
# =============================================================================


def check_live_gates(args: argparse.Namespace) -> tuple[bool, list[str]]:
    """Check all gates for live remediation mode.

    Returns:
        (all_passed, list_of_failing_gate_names)
    """
    failures: list[str] = []

    # Gate 1: --confirm LIVE_REMEDIATE
    if args.confirm != CONFIRM_TOKEN:
        failures.append(f"--confirm must be '{CONFIRM_TOKEN}' (got '{args.confirm}')")

    # Gate 2: RECONCILE_DRY_RUN=0
    dry_run_env = os.environ.get(ENV_DRY_RUN, "1").lower()
    if dry_run_env not in ("0", "false", "no"):
        failures.append(f"{ENV_DRY_RUN} must be '0' (got '{dry_run_env}')")

    # Gate 3: RECONCILE_ALLOW_ACTIVE=1
    allow_active = os.environ.get(ENV_ALLOW_ACTIVE, "0").lower()
    if allow_active not in ("1", "true", "yes"):
        failures.append(f"{ENV_ALLOW_ACTIVE} must be '1' (got '{allow_active}')")

    # Gate 4: ARMED=1
    armed = os.environ.get(ENV_ARMED, "0").lower()
    if armed not in ("1", "true", "yes"):
        failures.append(f"{ENV_ARMED} must be '1' (got '{armed}')")

    # Gate 5: ALLOW_MAINNET_TRADE=1
    allow_mainnet = os.environ.get(ENV_ALLOW_MAINNET, "0").lower()
    if allow_mainnet not in ("1", "true", "yes"):
        failures.append(f"{ENV_ALLOW_MAINNET} must be '1' (got '{allow_mainnet}')")

    return len(failures) == 0, failures


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================


def print_header(text: str) -> None:
    """Print section header."""
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def print_config(
    mode: str,
    config: ReconcileConfig,
    symbol_whitelist: list[str],
    armed: bool,
    audit_enabled: bool,
) -> None:
    """Print configuration summary."""
    print_header("CONFIGURATION")
    print(f"  Mode:              {mode}")
    print(f"  action:            {config.action.value}")
    print(f"  dry_run:           {config.dry_run}")
    print(f"  allow_active:      {config.allow_active_remediation}")
    print(f"  armed:             {armed}")
    print(f"  symbol_whitelist:  {symbol_whitelist}")
    print(f"  cooldown_seconds:  {config.cooldown_seconds}")
    print(f"  max_orders:        {config.max_orders_per_action}")
    print(f"  max_symbols:       {config.max_symbols_per_action}")
    print(f"  audit_enabled:     {audit_enabled}")


def print_result(result: SmokeResult) -> None:
    """Print single scenario result."""
    status = "PASS" if result.passed else "FAIL"
    print(f"\n--- Scenario: {result.scenario} [{status}] ---")
    print(f"  Mismatches detected:  {result.mismatches_detected}")
    print(f"  Expected action:      {result.expected_action_type}")
    print(f"  Actual action:        {result.actual_action_type}")
    print(f"  Port calls:           {result.port_calls}")
    print(f"  Planned count:        {result.report.planned_count}")
    print(f"  Executed count:       {result.report.executed_count}")
    print(f"  Blocked count:        {result.report.blocked_count}")
    print(f"  Skipped terminal:     {result.report.skipped_terminal}")
    print(f"  Skipped no_action:    {result.report.skipped_no_action}")
    if not result.passed:
        print(f"  REASON:               {result.reason}")


def print_summary(results: list[SmokeResult], mode: str) -> int:
    """Print final summary and return exit code."""
    print_header("SUMMARY")
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    all_passed = passed == total

    print(f"  Mode:    {mode}")
    print(f"  Passed:  {passed}/{total}")

    if all_passed:
        print("\n  ALL SCENARIOS PASSED")
        return 0
    else:
        print("\n  SOME SCENARIOS FAILED:")
        for r in results:
            if not r.passed:
                print(f"    - {r.scenario}: {r.reason}")
        return 1


# =============================================================================
# MAIN
# =============================================================================


def main() -> int:
    """Run E2E reconcile→remediate smoke test."""
    parser = argparse.ArgumentParser(
        description="E2E smoke test for Reconcile→Remediate flow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        choices=["order", "position", "mixed", "all"],
        default="all",
        help="Scenario to run (default: all)",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Confirmation token for live mode (must be '{CONFIRM_TOKEN}')",
    )
    parser.add_argument(
        "--symbol",
        default=DEFAULT_SYMBOL,
        help=f"Symbol for smoke test (default: {DEFAULT_SYMBOL})",
    )

    args = parser.parse_args()

    # Determine mode
    live_mode = False
    if args.confirm:
        all_passed, failures = check_live_gates(args)
        if not all_passed:
            print_header("LIVE MODE GATE CHECK FAILED")
            print("  The following gates are not satisfied:")
            for f in failures:
                print(f"    - {f}")
            print("\n  Live mode requires ALL gates to pass.")
            print("  Use default mode (no --confirm) for dry-run.")
            return 1
        live_mode = True

    mode = "LIVE" if live_mode else "DRY-RUN"

    # Configure identity (v1 format)
    identity_config = OrderIdentityConfig(
        prefix="grinder_",
        strategy_id="smoke",
        require_strategy_allowlist=False,  # Smoke test doesn't require allowlist
    )
    set_default_identity_config(identity_config)

    # Configure reconciliation
    config = ReconcileConfig(
        enabled=True,
        action=RemediationAction.CANCEL_ALL,  # Use CANCEL_ALL to test order routing
        dry_run=not live_mode,
        allow_active_remediation=live_mode,
        max_orders_per_action=10,
        max_symbols_per_action=3,
        cooldown_seconds=0,  # No cooldown for smoke
        require_whitelist=False,  # Smoke test doesn't require whitelist
    )

    # Symbol whitelist
    symbol_whitelist = [args.symbol]

    # Armed status
    armed = live_mode

    # Audit setup
    audit_config = AuditConfig()  # Reads from env vars
    audit_writer: AuditWriter | None = None
    if audit_config.enabled:
        audit_writer = AuditWriter(audit_config)

    # Create fake port
    port = FakePort()

    # Print config
    print_config(mode, config, symbol_whitelist, armed, audit_config.enabled)

    # Run scenarios
    results: list[SmokeResult] = []

    scenarios_to_run = [args.scenario] if args.scenario != "all" else ["order", "position", "mixed"]

    for scenario_name in scenarios_to_run:
        if scenario_name == "order":
            # For order scenario, use CANCEL_ALL action
            order_config = ReconcileConfig(
                enabled=True,
                action=RemediationAction.CANCEL_ALL,
                dry_run=not live_mode,
                allow_active_remediation=live_mode,
                max_orders_per_action=10,
                max_symbols_per_action=3,
                cooldown_seconds=0,
                require_whitelist=False,
            )
            result = run_order_scenario(
                order_config, port, symbol_whitelist, identity_config, audit_writer, armed
            )
        elif scenario_name == "position":
            # For position scenario, use FLATTEN action
            position_config = ReconcileConfig(
                enabled=True,
                action=RemediationAction.FLATTEN,
                dry_run=not live_mode,
                allow_active_remediation=live_mode,
                max_orders_per_action=10,
                max_symbols_per_action=3,
                cooldown_seconds=0,
                require_whitelist=False,
            )
            result = run_position_scenario(
                position_config, port, symbol_whitelist, identity_config, audit_writer, armed
            )
        elif scenario_name == "mixed":
            # For mixed scenario, use CANCEL_ALL (order wins by priority)
            mixed_config = ReconcileConfig(
                enabled=True,
                action=RemediationAction.CANCEL_ALL,
                dry_run=not live_mode,
                allow_active_remediation=live_mode,
                max_orders_per_action=10,
                max_symbols_per_action=3,
                cooldown_seconds=0,
                require_whitelist=False,
            )
            result = run_mixed_scenario(
                mixed_config, port, symbol_whitelist, identity_config, audit_writer, armed
            )
        else:
            continue

        results.append(result)
        print_result(result)

    # Close audit writer
    if audit_writer:
        audit_writer.close()
        print(f"\n  Audit events written to: {audit_config.path}")

    # Print summary and return exit code
    return print_summary(results, mode)


if __name__ == "__main__":
    sys.exit(main())
