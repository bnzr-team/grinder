"""Reconciliation runner that wires engine to remediation.

See ADR-044 for design decisions.
See ADR-046 for audit trail design.

Orchestration flow:
1. ReconcileEngine.reconcile() → list[Mismatch]
2. Route mismatch → action via ROUTING_POLICY
3. RemediationExecutor.remediate_cancel/flatten() → RemediationResult
4. Emit audit logs (JSONL) and update metrics
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.core import OrderState
from grinder.reconcile.audit import AuditWriter, create_reconcile_run_event
from grinder.reconcile.metrics import get_reconcile_metrics
from grinder.reconcile.remediation import (
    RemediationExecutor,
    RemediationResult,
    RemediationStatus,
)
from grinder.reconcile.types import Mismatch, MismatchType, ObservedOrder, ObservedPosition

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from grinder.reconcile.engine import ReconcileEngine
    from grinder.reconcile.observed_state import ObservedStateStore

logger = logging.getLogger(__name__)


# =============================================================================
# ROUTING POLICY (SSOT) — See ADR-044
# =============================================================================

# Mismatches that route to CANCEL action
ORDER_MISMATCHES_FOR_CANCEL: frozenset[MismatchType] = frozenset(
    {
        MismatchType.ORDER_EXISTS_UNEXPECTED,
        MismatchType.ORDER_STATUS_DIVERGENCE,
    }
)

# Mismatches that route to FLATTEN action
POSITION_MISMATCHES_FOR_FLATTEN: frozenset[MismatchType] = frozenset(
    {
        MismatchType.POSITION_NONZERO_UNEXPECTED,
    }
)

# Mismatches with NO ACTION in v0.1 (passive alert only)
NO_ACTION_MISMATCHES: frozenset[MismatchType] = frozenset(
    {
        MismatchType.ORDER_MISSING_ON_EXCHANGE,
    }
)

# Terminal order statuses - no cancel action needed
TERMINAL_STATUSES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)

# Actionable order statuses - can be cancelled
ACTIONABLE_STATUSES: frozenset[OrderState] = frozenset(
    {
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
    }
)

# Priority for deterministic mismatch ordering (lower = higher priority)
# Cancel actions (order mismatches) have higher priority than flatten (position)
MISMATCH_PRIORITY: dict[MismatchType, int] = {
    MismatchType.ORDER_EXISTS_UNEXPECTED: 10,
    MismatchType.ORDER_STATUS_DIVERGENCE: 20,
    MismatchType.ORDER_MISSING_ON_EXCHANGE: 90,
    MismatchType.POSITION_NONZERO_UNEXPECTED: 100,
}


def _mismatch_sort_key(m: Mismatch) -> tuple[int, str, str]:
    """Generate sort key for deterministic mismatch ordering.

    Order: priority (lower first) → symbol → client_order_id
    """
    priority = MISMATCH_PRIORITY.get(m.mismatch_type, 999)
    return (priority, m.symbol, m.client_order_id or "")


# =============================================================================
# RUN REPORT
# =============================================================================


@dataclass(frozen=True)
class ReconcileRunReport:
    """Report from a single reconciliation run.

    Attributes:
        ts_start: Run start timestamp (ms)
        ts_end: Run end timestamp (ms)
        mismatches_detected: Total mismatches from engine
        cancel_results: Results from cancel actions
        flatten_results: Results from flatten actions
        skipped_terminal: Mismatches skipped due to terminal status
        skipped_no_action: Mismatches with no action (ORDER_MISSING_ON_EXCHANGE)
    """

    ts_start: int
    ts_end: int
    mismatches_detected: int
    cancel_results: tuple[RemediationResult, ...]
    flatten_results: tuple[RemediationResult, ...]
    skipped_terminal: int
    skipped_no_action: int

    @property
    def total_actions(self) -> int:
        """Total remediation actions (planned, executed, or blocked)."""
        return len(self.cancel_results) + len(self.flatten_results)

    @property
    def executed_count(self) -> int:
        """Count of actually executed actions."""
        return sum(
            1
            for r in (*self.cancel_results, *self.flatten_results)
            if r.status == RemediationStatus.EXECUTED
        )

    @property
    def planned_count(self) -> int:
        """Count of dry-run planned actions."""
        return sum(
            1
            for r in (*self.cancel_results, *self.flatten_results)
            if r.status == RemediationStatus.PLANNED
        )

    @property
    def blocked_count(self) -> int:
        """Count of blocked actions."""
        return sum(
            1
            for r in (*self.cancel_results, *self.flatten_results)
            if r.status == RemediationStatus.BLOCKED
        )

    def to_log_extra(self) -> dict[str, int]:
        """Generate extra dict for structured logging."""
        return {
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
            "duration_ms": self.ts_end - self.ts_start,
            "mismatches_detected": self.mismatches_detected,
            "cancel_count": len(self.cancel_results),
            "flatten_count": len(self.flatten_results),
            "executed_count": self.executed_count,
            "planned_count": self.planned_count,
            "blocked_count": self.blocked_count,
            "skipped_terminal": self.skipped_terminal,
            "skipped_no_action": self.skipped_no_action,
        }


# =============================================================================
# RECONCILE RUNNER
# =============================================================================


@dataclass
class ReconcileRunner:
    """Orchestrates reconciliation: detect → route → remediate.

    Flow:
    1. Call engine.reconcile() to detect mismatches
    2. Route each mismatch to action via ROUTING_POLICY
    3. Execute remediation via executor
    4. Return report with results

    Bounded execution:
    - One action type per run (cancel OR flatten, whichever comes first)
    - Honors executor's max_orders_per_action / max_symbols_per_action
    - Respects cooldown between runs

    Thread-safety: No (use separate instances per thread)

    Attributes:
        engine: ReconcileEngine for mismatch detection
        executor: RemediationExecutor for actions
        observed: ObservedStateStore for resolving observed entities
        price_getter: Function to get current price for notional calculation
    """

    engine: ReconcileEngine
    executor: RemediationExecutor
    observed: ObservedStateStore
    price_getter: Callable[[str], Decimal] | None = None
    audit_writer: AuditWriter | None = None

    _clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))

    def run(self) -> ReconcileRunReport:  # noqa: PLR0912, PLR0915
        """Execute one reconciliation run.

        Returns:
            ReconcileRunReport with all results and metrics
        """
        ts_start = self._clock()
        metrics = get_reconcile_metrics()

        # Generate audit run_id if audit is enabled
        run_id: str | None = None
        if self.audit_writer is not None:
            run_id = self.audit_writer.start_run()

        # Reset per-run counters in executor
        self.executor.reset_run_counters()

        # Step 1: Detect mismatches
        mismatches = self.engine.reconcile()
        mismatches_detected = len(mismatches)

        # Collect mismatch counts by type and symbols for audit
        mismatch_counts: dict[str, int] = {}
        symbols_with_mismatches: list[str] = []
        for m in mismatches:
            mt = m.mismatch_type.value
            mismatch_counts[mt] = mismatch_counts.get(mt, 0) + 1
            symbols_with_mismatches.append(m.symbol)

        # Update runs_with_mismatch metric
        if mismatches_detected > 0:
            metrics.record_run_with_mismatch()

        # Step 1.5: Sort for deterministic action-type locking
        # Lower priority = processed first (cancel before flatten)
        mismatches_sorted = sorted(mismatches, key=_mismatch_sort_key)

        # Step 2: Route and execute
        cancel_results: list[RemediationResult] = []
        flatten_results: list[RemediationResult] = []
        skipped_terminal = 0
        skipped_no_action = 0
        action_type_locked: str | None = None  # "cancel" or "flatten"

        for mismatch in mismatches_sorted:
            # Determine action type
            if mismatch.mismatch_type in NO_ACTION_MISMATCHES:
                skipped_no_action += 1
                self._log_skip(mismatch, "no_action_v0.1")
                continue

            if mismatch.mismatch_type in ORDER_MISMATCHES_FOR_CANCEL:
                # Check if we should skip (locked to flatten)
                if action_type_locked == "flatten":
                    self._log_skip(mismatch, "action_type_locked_to_flatten")
                    continue

                # Resolve observed order
                result = self._handle_cancel(mismatch)
                if result is None:
                    skipped_terminal += 1
                    continue

                cancel_results.append(result)
                action_type_locked = "cancel"

                # Update metrics for real execution
                if result.status == RemediationStatus.EXECUTED:
                    metrics.record_run_with_remediation("cancel_all")
                    metrics.set_last_remediation_ts(self._clock())

            elif mismatch.mismatch_type in POSITION_MISMATCHES_FOR_FLATTEN:
                # Check if we should skip (locked to cancel)
                if action_type_locked == "cancel":
                    self._log_skip(mismatch, "action_type_locked_to_cancel")
                    continue

                result = self._handle_flatten(mismatch)
                if result is None:
                    continue

                flatten_results.append(result)
                action_type_locked = "flatten"

                # Update metrics for real execution
                if result.status == RemediationStatus.EXECUTED:
                    metrics.record_run_with_remediation("flatten")
                    metrics.set_last_remediation_ts(self._clock())

        ts_end = self._clock()

        # Build report
        report = ReconcileRunReport(
            ts_start=ts_start,
            ts_end=ts_end,
            mismatches_detected=mismatches_detected,
            cancel_results=tuple(cancel_results),
            flatten_results=tuple(flatten_results),
            skipped_terminal=skipped_terminal,
            skipped_no_action=skipped_no_action,
        )

        # Structured log
        self._log_run_complete(report)

        # Write audit event (JSONL)
        if self.audit_writer is not None and run_id is not None:
            self._write_audit_event(report, run_id, mismatch_counts, symbols_with_mismatches)

        return report

    def _handle_cancel(self, mismatch: Mismatch) -> RemediationResult | None:
        """Handle cancel action for order mismatch.

        Returns:
            RemediationResult or None if skipped (terminal status)
        """
        client_order_id = mismatch.client_order_id
        if client_order_id is None:
            logger.warning(
                "REMEDIATE_SKIP",
                extra={
                    "mismatch_type": mismatch.mismatch_type.value,
                    "symbol": mismatch.symbol,
                    "reason": "no_client_order_id",
                },
            )
            return None

        # Resolve observed order
        observed_order = self.observed.get_order(client_order_id)
        if observed_order is None:
            # Try to reconstruct from mismatch.observed dict
            if mismatch.observed is not None:
                observed_order = ObservedOrder.from_dict(mismatch.observed)
            else:
                logger.warning(
                    "REMEDIATE_SKIP",
                    extra={
                        "mismatch_type": mismatch.mismatch_type.value,
                        "symbol": mismatch.symbol,
                        "client_order_id": client_order_id,
                        "reason": "order_not_found",
                    },
                )
                return None

        # Check terminal status
        if observed_order.status in TERMINAL_STATUSES:
            self._log_skip(mismatch, "terminal_status")
            return None

        # Check actionable status
        if observed_order.status not in ACTIONABLE_STATUSES:
            self._log_skip(mismatch, f"non_actionable_status_{observed_order.status.value}")
            return None

        # Execute cancel
        return self.executor.remediate_cancel(observed_order)

    def _handle_flatten(self, mismatch: Mismatch) -> RemediationResult | None:
        """Handle flatten action for position mismatch.

        Returns:
            RemediationResult or None if skipped
        """
        # Resolve observed position
        observed_position = self.observed.get_position(mismatch.symbol)
        if observed_position is None:
            # Try to reconstruct from mismatch.observed dict
            if mismatch.observed is not None:
                observed_position = ObservedPosition.from_dict(mismatch.observed)
            else:
                logger.warning(
                    "REMEDIATE_SKIP",
                    extra={
                        "mismatch_type": mismatch.mismatch_type.value,
                        "symbol": mismatch.symbol,
                        "reason": "position_not_found",
                    },
                )
                return None

        # Get current price for notional calculation
        if self.price_getter is not None:
            current_price = self.price_getter(mismatch.symbol)
        else:
            # Fallback to entry price if no price_getter
            current_price = observed_position.entry_price

        return self.executor.remediate_flatten(observed_position, current_price)

    def _log_skip(self, mismatch: Mismatch, reason: str) -> None:
        """Log skipped mismatch."""
        logger.info(
            "REMEDIATE_SKIP",
            extra={
                "mismatch_type": mismatch.mismatch_type.value,
                "symbol": mismatch.symbol,
                "client_order_id": mismatch.client_order_id,
                "reason": reason,
            },
        )

    def _log_run_complete(self, report: ReconcileRunReport) -> None:
        """Log run completion with summary."""
        logger.info(
            "RECONCILE_RUN",
            extra=report.to_log_extra(),
        )

    def _write_audit_event(
        self,
        report: ReconcileRunReport,
        run_id: str,
        mismatch_counts: dict[str, int],
        symbols: list[str],
    ) -> None:
        """Write RECONCILE_RUN event to audit trail."""
        if self.audit_writer is None:
            return

        # Determine mode from executor config
        mode = "dry_run" if self.executor.config.dry_run else "live"
        action = self.executor.config.action.value

        event = create_reconcile_run_event(
            run_id=run_id,
            ts_start=report.ts_start,
            ts_end=report.ts_end,
            mode=mode,
            action=action,
            mismatch_counts=mismatch_counts,
            symbols=symbols,
            cancel_count=len(report.cancel_results),
            flatten_count=len(report.flatten_results),
            executed_count=report.executed_count,
            planned_count=report.planned_count,
            blocked_count=report.blocked_count,
            skipped_terminal=report.skipped_terminal,
            skipped_no_action=report.skipped_no_action,
        )

        self.audit_writer.write(event)
