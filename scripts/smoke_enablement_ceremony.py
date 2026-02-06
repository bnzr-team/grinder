#!/usr/bin/env python3
"""Smoke test for enablement ceremony stages (LC-15a).

Tests each stage of the enablement ceremony without live execution:
- Stage A: Detect-only (action=NONE)
- Stage B: Plan-only (dry_run=True)
- Stage C: Blocked (gates prevent execution)
- Stage D: (Optional) Controlled execution with --confirm

Default mode: ZERO execution calls (safe to run anytime).

Usage:
    # Default: verify stages A/B/C work, no execution
    PYTHONPATH=src python3 -m scripts.smoke_enablement_ceremony

    # With mismatch injection to see plans/blocks
    PYTHONPATH=src python3 -m scripts.smoke_enablement_ceremony --inject-mismatch

    # Verbose output
    PYTHONPATH=src python3 -m scripts.smoke_enablement_ceremony -v
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal  # noqa: TC003 - used at runtime in type hints
from typing import Any

# Configure logging first
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# =============================================================================
# FakePort for execution tracking
# =============================================================================


@dataclass
class FakePort:
    """Fake port that records calls without executing.

    Used to verify that ceremony stages don't trigger real execution.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """Record cancel attempt."""
        call = {
            "action": "cancel_order",
            "symbol": symbol,
            "client_order_id": client_order_id,
            "ts": int(time.time() * 1000),
        }
        self.calls.append(call)
        logger.warning("FAKE_PORT_CANCEL", extra=call)
        return {"status": "CANCELED", "clientOrderId": client_order_id}

    def place_market_order(self, symbol: str, side: str, quantity: Decimal) -> dict[str, Any]:
        """Record market order attempt."""
        call = {
            "action": "place_market_order",
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "ts": int(time.time() * 1000),
        }
        self.calls.append(call)
        logger.warning("FAKE_PORT_PLACE", extra=call)
        return {"orderId": "fake_123", "status": "FILLED"}


# =============================================================================
# Stage results
# =============================================================================


@dataclass
class StageResult:
    """Result of a ceremony stage."""

    name: str
    passed: bool
    port_calls: int
    planned_count: int = 0
    executed_count: int = 0
    blocked_count: int = 0
    blocked_reasons: list[str] = field(default_factory=list)
    error: str | None = None


# =============================================================================
# Ceremony stages
# =============================================================================


def run_stage_a_detect_only(
    fake_port: FakePort,
    inject_mismatch: bool = False,
) -> StageResult:
    """Stage A: Detect-only mode (action=NONE).

    Expected: runs execute, but no remediation planned/executed.
    """
    from grinder.reconcile.config import ReconcileConfig, RemediationAction  # noqa: PLC0415
    from grinder.reconcile.engine import ReconcileEngine  # noqa: PLC0415
    from grinder.reconcile.expected_state import ExpectedStateStore  # noqa: PLC0415
    from grinder.reconcile.identity import OrderIdentityConfig  # noqa: PLC0415
    from grinder.reconcile.observed_state import ObservedStateStore  # noqa: PLC0415
    from grinder.reconcile.remediation import RemediationExecutor  # noqa: PLC0415
    from grinder.reconcile.runner import ReconcileRunner  # noqa: PLC0415

    logger.info("=== Stage A: Detect-only (action=NONE) ===")

    try:
        expected = ExpectedStateStore()
        observed = ObservedStateStore()
        identity_config = OrderIdentityConfig(prefix="grinder_ceremony", allowed_strategies={"1"})

        # Detect-only config
        config = ReconcileConfig(
            action=RemediationAction.NONE,  # No remediation
            dry_run=True,
            allow_active_remediation=False,
        )
        engine = ReconcileEngine(
            expected=expected,
            observed=observed,
            config=config,
            identity_config=identity_config,
        )

        executor = RemediationExecutor(
            config=config,
            port=fake_port,  # type: ignore[arg-type]
            armed=False,
            symbol_whitelist=["BTCUSDT"],
        )

        runner = ReconcileRunner(
            engine=engine,
            executor=executor,
            observed=observed,
        )

        # Optionally inject mismatch
        if inject_mismatch:
            ts_now = int(time.time() * 1000)
            observed.update_from_rest_orders(
                [
                    {
                        "symbol": "BTCUSDT",
                        "clientOrderId": "grinder_ceremony_unexpected_001",
                        "orderId": 12345,
                        "side": "BUY",
                        "price": "50000",
                        "origQty": "0.001",
                        "executedQty": "0",
                        "avgPrice": "0",
                        "status": "NEW",
                    }
                ],
                ts=ts_now,
            )
            logger.info("Injected unexpected order for testing")

        # Run reconciliation
        report = runner.run()

        port_calls_before = len(fake_port.calls)

        return StageResult(
            name="A: Detect-only",
            passed=report.executed_count == 0 and port_calls_before == 0,
            port_calls=port_calls_before,
            planned_count=report.planned_count,
            executed_count=report.executed_count,
            blocked_count=report.blocked_count,
        )

    except Exception as e:
        logger.exception("Stage A failed")
        return StageResult(
            name="A: Detect-only",
            passed=False,
            port_calls=len(fake_port.calls),
            error=str(e),
        )


def run_stage_b_plan_only(
    fake_port: FakePort,
    inject_mismatch: bool = False,
) -> StageResult:
    """Stage B: Plan-only mode (dry_run=True).

    Expected: mismatches detected, plans created, but not executed.
    """
    from grinder.reconcile.config import ReconcileConfig, RemediationAction  # noqa: PLC0415
    from grinder.reconcile.engine import ReconcileEngine  # noqa: PLC0415
    from grinder.reconcile.expected_state import ExpectedStateStore  # noqa: PLC0415
    from grinder.reconcile.identity import OrderIdentityConfig  # noqa: PLC0415
    from grinder.reconcile.observed_state import ObservedStateStore  # noqa: PLC0415
    from grinder.reconcile.remediation import RemediationExecutor  # noqa: PLC0415
    from grinder.reconcile.runner import ReconcileRunner  # noqa: PLC0415

    logger.info("=== Stage B: Plan-only (dry_run=True) ===")

    try:
        expected = ExpectedStateStore()
        observed = ObservedStateStore()
        identity_config = OrderIdentityConfig(prefix="grinder_ceremony", allowed_strategies={"1"})

        # Plan-only config
        config = ReconcileConfig(
            action=RemediationAction.CANCEL_ALL,  # Would cancel, but dry_run
            dry_run=True,  # Plans only
            allow_active_remediation=True,  # Allow planning
        )

        engine = ReconcileEngine(
            expected=expected,
            observed=observed,
            config=config,
            identity_config=identity_config,
        )

        executor = RemediationExecutor(
            config=config,
            port=fake_port,  # type: ignore[arg-type]
            armed=False,  # Extra safety
            symbol_whitelist=["BTCUSDT"],
        )

        runner = ReconcileRunner(
            engine=engine,
            executor=executor,
            observed=observed,
        )

        # Optionally inject mismatch
        if inject_mismatch:
            ts_now = int(time.time() * 1000)
            observed.update_from_rest_orders(
                [
                    {
                        "symbol": "BTCUSDT",
                        "clientOrderId": "grinder_ceremony_unexpected_002",
                        "orderId": 12346,
                        "side": "BUY",
                        "price": "50000",
                        "origQty": "0.001",
                        "executedQty": "0",
                        "avgPrice": "0",
                        "status": "NEW",
                    }
                ],
                ts=ts_now,
            )
            logger.info("Injected unexpected order for testing")

        port_calls_before = len(fake_port.calls)
        report = runner.run()
        port_calls_after = len(fake_port.calls)

        return StageResult(
            name="B: Plan-only",
            passed=report.executed_count == 0 and port_calls_after == port_calls_before,
            port_calls=port_calls_after - port_calls_before,
            planned_count=report.planned_count,
            executed_count=report.executed_count,
            blocked_count=report.blocked_count,
        )

    except Exception as e:
        logger.exception("Stage B failed")
        return StageResult(
            name="B: Plan-only",
            passed=False,
            port_calls=0,
            error=str(e),
        )


def run_stage_c_blocked(
    fake_port: FakePort,
    inject_mismatch: bool = False,
) -> StageResult:
    """Stage C: Execution blocked (gates prevent execution).

    Expected: remediation attempted but blocked (armed=False, no env var).
    """
    from grinder.reconcile.config import ReconcileConfig, RemediationAction  # noqa: PLC0415
    from grinder.reconcile.engine import ReconcileEngine  # noqa: PLC0415
    from grinder.reconcile.expected_state import ExpectedStateStore  # noqa: PLC0415
    from grinder.reconcile.identity import OrderIdentityConfig  # noqa: PLC0415
    from grinder.reconcile.observed_state import ObservedStateStore  # noqa: PLC0415
    from grinder.reconcile.remediation import RemediationExecutor  # noqa: PLC0415
    from grinder.reconcile.runner import ReconcileRunner  # noqa: PLC0415

    logger.info("=== Stage C: Blocked (gates prevent execution) ===")

    try:
        expected = ExpectedStateStore()
        observed = ObservedStateStore()
        identity_config = OrderIdentityConfig(prefix="grinder_ceremony", allowed_strategies={"1"})

        # Would execute, but gates block
        config = ReconcileConfig(
            action=RemediationAction.CANCEL_ALL,
            dry_run=False,  # Not dry-run
            allow_active_remediation=True,
        )

        engine = ReconcileEngine(
            expected=expected,
            observed=observed,
            config=config,
            identity_config=identity_config,
        )

        # Gates will block: armed=False
        executor = RemediationExecutor(
            config=config,
            port=fake_port,  # type: ignore[arg-type]
            armed=False,  # BLOCKED: not armed
            symbol_whitelist=["BTCUSDT"],
        )

        runner = ReconcileRunner(
            engine=engine,
            executor=executor,
            observed=observed,
        )

        # Inject mismatch to trigger remediation attempt
        if inject_mismatch:
            ts_now = int(time.time() * 1000)
            observed.update_from_rest_orders(
                [
                    {
                        "symbol": "BTCUSDT",
                        "clientOrderId": "grinder_ceremony_unexpected_003",
                        "orderId": 12347,
                        "side": "BUY",
                        "price": "50000",
                        "origQty": "0.001",
                        "executedQty": "0",
                        "avgPrice": "0",
                        "status": "NEW",
                    }
                ],
                ts=ts_now,
            )
            logger.info("Injected unexpected order for testing")

        port_calls_before = len(fake_port.calls)
        report = runner.run()
        port_calls_after = len(fake_port.calls)

        # Collect blocked reasons
        blocked_reasons = []
        if report.blocked_count > 0:
            blocked_reasons.append("not_armed")  # We know armed=False

        return StageResult(
            name="C: Blocked",
            passed=report.executed_count == 0 and port_calls_after == port_calls_before,
            port_calls=port_calls_after - port_calls_before,
            planned_count=report.planned_count,
            executed_count=report.executed_count,
            blocked_count=report.blocked_count,
            blocked_reasons=blocked_reasons,
        )

    except Exception as e:
        logger.exception("Stage C failed")
        return StageResult(
            name="C: Blocked",
            passed=False,
            port_calls=0,
            error=str(e),
        )


# =============================================================================
# Main
# =============================================================================


def main() -> int:  # noqa: PLR0915
    """Run enablement ceremony smoke test."""
    parser = argparse.ArgumentParser(description="Enablement ceremony smoke test (LC-15a)")
    parser.add_argument(
        "--inject-mismatch",
        action="store_true",
        help="Inject test mismatch to trigger remediation logic",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    print()
    print("=" * 60)
    print("  LC-15a: ENABLEMENT CEREMONY SMOKE TEST")
    print("=" * 60)
    print(f"  Inject mismatch: {args.inject_mismatch}")
    print("=" * 60)
    print()

    # Shared FakePort to track all calls
    fake_port = FakePort()

    # Run stages
    results: list[StageResult] = []

    results.append(run_stage_a_detect_only(fake_port, args.inject_mismatch))
    results.append(run_stage_b_plan_only(fake_port, args.inject_mismatch))
    results.append(run_stage_c_blocked(fake_port, args.inject_mismatch))

    # Print results
    print()
    print("=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print()

    all_passed = True
    total_port_calls = len(fake_port.calls)

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"  {result.name}: {status}")
        print(f"    - Port calls: {result.port_calls}")
        print(f"    - Planned: {result.planned_count}")
        print(f"    - Executed: {result.executed_count}")
        print(f"    - Blocked: {result.blocked_count}")
        if result.blocked_reasons:
            print(f"    - Block reasons: {', '.join(result.blocked_reasons)}")
        if result.error:
            print(f"    - Error: {result.error}")
        print()

        if not result.passed:
            all_passed = False

    # Final verification
    print("=" * 60)
    print("  VERIFICATION")
    print("=" * 60)
    print()
    print(f"  Total port calls: {total_port_calls}")

    if total_port_calls == 0:
        print()
        print("=" * 60)
        print("  \u2713 DETECT-ONLY MODE VERIFIED: Zero port calls")
        print("=" * 60)
    else:
        print()
        print("=" * 60)
        print(f"  \u2717 EXECUTION DETECTED: {total_port_calls} port calls")
        print("=" * 60)
        all_passed = False

    if all_passed:
        print()
        print("  ALL STAGES PASSED")
        return 0
    else:
        print()
        print("  SOME STAGES FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
