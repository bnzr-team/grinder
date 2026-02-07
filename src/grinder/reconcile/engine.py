"""Reconciliation engine for mismatch detection.

See ADR-042 for design decisions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from grinder.core import OrderState
from grinder.reconcile.identity import (
    OrderIdentityConfig,
    get_default_identity_config,
    is_ours,
)
from grinder.reconcile.metrics import ReconcileMetrics
from grinder.reconcile.types import Mismatch, MismatchType

if TYPE_CHECKING:
    from collections.abc import Callable

    from grinder.reconcile.config import ReconcileConfig
    from grinder.reconcile.expected_state import ExpectedStateStore
    from grinder.reconcile.observed_state import ObservedStateStore

logger = logging.getLogger(__name__)


@dataclass
class ReconcileEngine:
    """Engine for detecting reconciliation mismatches.

    Compares ExpectedStateStore with ObservedStateStore and emits:
    - Structured logs (RECONCILE_MISMATCH)
    - Metrics updates
    - Mismatch objects with action plans (text only in v0.1)

    Mismatch types:
    - ORDER_MISSING_ON_EXCHANGE: Expected OPEN, not in observed (after grace period)
    - ORDER_EXISTS_UNEXPECTED: Observed OPEN, not in expected
    - ORDER_STATUS_DIVERGENCE: Expected vs observed status differs
    - POSITION_NONZERO_UNEXPECTED: Position != 0, expected = 0

    v0.1: Passive only - logs + metrics, no remediation actions.
    """

    config: ReconcileConfig
    expected: ExpectedStateStore
    observed: ObservedStateStore
    metrics: ReconcileMetrics = field(default_factory=ReconcileMetrics)
    identity_config: OrderIdentityConfig | None = None

    _clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))

    def reconcile(self) -> list[Mismatch]:
        """Run reconciliation check.

        Returns:
            List of detected mismatches
        """
        if not self.config.enabled:
            return []

        mismatches: list[Mismatch] = []
        now = self._clock()

        # 1. Check expected orders against observed
        mismatches.extend(self._check_expected_orders(now))

        # 2. Check observed orders against expected (unexpected orders)
        mismatches.extend(self._check_unexpected_orders(now))

        # 3. Check positions
        mismatches.extend(self._check_positions(now))

        # Update metrics
        for mismatch in mismatches:
            self.metrics.record_mismatch(mismatch.mismatch_type)

        # Track snapshot timestamp and age
        snapshot_ts = self.observed.last_snapshot_ts
        self.metrics.set_last_snapshot_ts(snapshot_ts)
        if snapshot_ts > 0:
            self.metrics.set_last_snapshot_age(now - snapshot_ts)
        # else: leave age at 0 (no snapshot yet)
        self.metrics.record_reconcile_run()

        return mismatches

    def _check_expected_orders(self, now: int) -> list[Mismatch]:
        """Check expected orders against observed state."""
        mismatches: list[Mismatch] = []

        for expected_order in self.expected.get_active_orders():
            observed_order = self.observed.get_order(expected_order.client_order_id)

            if observed_order is None:
                # Not yet observed - check grace period
                age_ms = now - expected_order.ts_created
                if age_ms > self.config.order_grace_period_ms:
                    mismatch = Mismatch(
                        mismatch_type=MismatchType.ORDER_MISSING_ON_EXCHANGE,
                        symbol=expected_order.symbol,
                        client_order_id=expected_order.client_order_id,
                        expected=expected_order.to_dict(),
                        observed=None,
                        ts_detected=now,
                        action_plan=(
                            f"would cancel order {expected_order.client_order_id} "
                            f"(missing on exchange after {age_ms}ms)"
                        ),
                    )
                    mismatches.append(mismatch)
                    self._log_mismatch(mismatch)

            elif expected_order.expected_status == OrderState.OPEN:
                # Expected OPEN - check for status divergence
                # Skip if observed is terminal (order completed normally)
                if observed_order.is_terminal():
                    continue

                # Check for unexpected status
                if observed_order.status not in (
                    OrderState.OPEN,
                    OrderState.PARTIALLY_FILLED,
                ):
                    mismatch = Mismatch(
                        mismatch_type=MismatchType.ORDER_STATUS_DIVERGENCE,
                        symbol=expected_order.symbol,
                        client_order_id=expected_order.client_order_id,
                        expected=expected_order.to_dict(),
                        observed=observed_order.to_dict(),
                        ts_detected=now,
                        action_plan=(
                            f"status divergence: "
                            f"expected={expected_order.expected_status.value}, "
                            f"observed={observed_order.status.value}"
                        ),
                    )
                    mismatches.append(mismatch)
                    self._log_mismatch(mismatch)

        return mismatches

    def _check_unexpected_orders(self, now: int) -> list[Mismatch]:
        """Check for unexpected orders on exchange."""
        mismatches: list[Mismatch] = []

        expected_cids = {o.client_order_id for o in self.expected.get_all_orders()}

        # LC-12: Use identity config for ownership check
        identity = self.identity_config or get_default_identity_config()

        for observed_order in self.observed.get_open_orders():
            # Skip orders that don't match our identity (prefix + strategy allowlist)
            if not is_ours(observed_order.client_order_id, identity):
                continue

            if observed_order.client_order_id not in expected_cids:
                mismatch = Mismatch(
                    mismatch_type=MismatchType.ORDER_EXISTS_UNEXPECTED,
                    symbol=observed_order.symbol,
                    client_order_id=observed_order.client_order_id,
                    expected=None,
                    observed=observed_order.to_dict(),
                    ts_detected=now,
                    action_plan=(f"would cancel unexpected order {observed_order.client_order_id}"),
                )
                mismatches.append(mismatch)
                self._log_mismatch(mismatch)

        return mismatches

    def _check_positions(self, now: int) -> list[Mismatch]:
        """Check positions for unexpected state."""
        mismatches: list[Mismatch] = []

        for expected_pos in self.expected.get_all_positions():
            observed_pos = self.observed.get_position(expected_pos.symbol)

            if observed_pos is None:
                continue

            # v0.1: Expected is always 0, check for nonzero
            if observed_pos.position_amt != Decimal(
                "0"
            ) and expected_pos.expected_position_amt == Decimal("0"):
                mismatch = Mismatch(
                    mismatch_type=MismatchType.POSITION_NONZERO_UNEXPECTED,
                    symbol=expected_pos.symbol,
                    client_order_id=None,
                    expected=expected_pos.to_dict(),
                    observed=observed_pos.to_dict(),
                    ts_detected=now,
                    action_plan=(
                        f"would flatten position {expected_pos.symbol} "
                        f"(observed={observed_pos.position_amt})"
                    ),
                )
                mismatches.append(mismatch)
                self._log_mismatch(mismatch)

        return mismatches

    def _log_mismatch(self, mismatch: Mismatch) -> None:
        """Log mismatch with structured data."""
        logger.warning(
            "RECONCILE_MISMATCH",
            extra=mismatch.to_log_extra(),
        )
