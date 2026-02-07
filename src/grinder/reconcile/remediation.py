"""Active remediation executor for reconciliation mismatches.

See ADR-043 for design decisions.
See ADR-045 for configurable order identity (LC-12).

Key safety guarantees:
- 9 safety gates must ALL pass for real execution
- Default: dry-run only (plan but don't execute)
- Order identity check required for cancel (protects manual/other orders)
- Notional cap required for flatten (limits exposure)
- Kill-switch ALLOWS remediation (reduces risk)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from grinder.core import OrderSide
from grinder.reconcile.budget import BudgetTracker
from grinder.reconcile.config import ReconcileConfig, RemediationAction, RemediationMode
from grinder.reconcile.identity import (
    OrderIdentityConfig,
    get_default_identity_config,
    is_ours,
    parse_client_order_id,
)
from grinder.reconcile.metrics import get_reconcile_metrics
from grinder.reconcile.types import MismatchType, ObservedOrder, ObservedPosition

if TYPE_CHECKING:
    from grinder.execution.binance_futures_port import BinanceFuturesPort

logger = logging.getLogger(__name__)

# Legacy prefix constant - kept for backward compatibility
# LC-12: Actual prefix is now configurable via OrderIdentityConfig
GRINDER_PREFIX = "grinder_"


class RemediationBlockReason(Enum):
    """Reasons why remediation was blocked.

    These values are STABLE and used as metric labels.
    DO NOT rename or remove values without updating metric contracts.
    """

    # Original LC-10 reasons
    ACTION_IS_NONE = "action_is_none"
    DRY_RUN = "dry_run"
    NOT_ALLOWED = "allow_active_remediation_false"
    NOT_ARMED = "not_armed"
    ENV_VAR_MISSING = "env_var_missing"
    COOLDOWN_NOT_ELAPSED = "cooldown_not_elapsed"
    SYMBOL_NOT_IN_WHITELIST = "symbol_not_in_whitelist"
    NO_GRINDER_PREFIX = "no_grinder_prefix"
    NOTIONAL_EXCEEDS_LIMIT = "notional_exceeds_limit"
    MAX_ORDERS_REACHED = "max_orders_reached"
    MAX_SYMBOLS_REACHED = "max_symbols_reached"
    WHITELIST_REQUIRED = "whitelist_required"
    PORT_ERROR = "port_error"

    # LC-18: Mode-based reasons
    MODE_DETECT_ONLY = "mode_detect_only"
    MODE_PLAN_ONLY = "mode_plan_only"
    MODE_BLOCKED = "mode_blocked"
    MODE_CANCEL_ONLY = "mode_cancel_only"  # flatten not allowed in execute_cancel_all
    MODE_FLATTEN_ONLY = "mode_flatten_only"  # cancel not allowed in execute_flatten

    # LC-18: Budget reasons
    MAX_CALLS_PER_RUN = "max_calls_per_run"
    MAX_NOTIONAL_PER_RUN = "max_notional_per_run"
    MAX_CALLS_PER_DAY = "max_calls_per_day"
    MAX_NOTIONAL_PER_DAY = "max_notional_per_day"

    # LC-18: Allowlist reasons
    STRATEGY_NOT_ALLOWED = "strategy_not_allowed"
    SYMBOL_NOT_IN_REMEDIATION_ALLOWLIST = "symbol_not_in_remediation_allowlist"


class RemediationStatus(Enum):
    """Status of a remediation action."""

    PLANNED = "planned"  # Dry-run: would execute if enabled
    EXECUTED = "executed"  # Real execution succeeded
    BLOCKED = "blocked"  # Safety gate blocked execution
    FAILED = "failed"  # Port error during execution


@dataclass(frozen=True)
class RemediationResult:
    """Result of a remediation attempt.

    Attributes:
        mismatch_type: Type of mismatch being remediated
        symbol: Trading symbol
        client_order_id: Order ID (for cancel) or None (for flatten)
        status: Result status
        block_reason: Why blocked (if status=BLOCKED)
        error: Error message (if status=FAILED)
        action: Action type string ("cancel_all" or "flatten")
    """

    mismatch_type: str
    symbol: str
    client_order_id: str | None
    status: RemediationStatus
    block_reason: RemediationBlockReason | None = None
    error: str | None = None
    action: str = ""

    def to_log_extra(self) -> dict[str, str | None]:
        """Generate extra dict for structured logging."""
        return {
            "mismatch_type": self.mismatch_type,
            "symbol": self.symbol,
            "client_order_id": self.client_order_id,
            "status": self.status.value,
            "block_reason": self.block_reason.value if self.block_reason else None,
            "action": self.action,
            "error": self.error,
        }


@dataclass
class RemediationExecutor:
    """Executor for active remediation actions.

    Safety architecture (LC-10 + LC-18):
    - Mode gates: DETECT_ONLY/PLAN_ONLY/BLOCKED prevent execution
    - Budget gates: Per-run and per-day limits
    - Allowlist gates: Strategy and symbol restrictions
    - Original 9 gates for backward compatibility
    - Kill-switch ALLOWS remediation (reduces risk exposure)
    - Default: detect_only mode (no planning, no execution)

    Thread-safety: No (use separate instances per thread)
    """

    config: ReconcileConfig
    port: BinanceFuturesPort
    armed: bool = False
    symbol_whitelist: list[str] = field(default_factory=list)
    kill_switch_active: bool = False
    identity_config: OrderIdentityConfig | None = None

    # LC-18: Budget tracker (initialized in __post_init__)
    budget_tracker: BudgetTracker | None = field(default=None, repr=False)

    # Internal state
    _last_action_ts: int = field(default=0, repr=False)
    _orders_this_run: int = field(default=0, repr=False)
    _symbols_this_run: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        """Initialize budget tracker from config."""
        if self.budget_tracker is None:
            self.budget_tracker = BudgetTracker(
                max_calls_per_day=self.config.max_calls_per_day,
                max_notional_per_day=self.config.max_notional_per_day,
                max_calls_per_run=self.config.max_calls_per_run,
                max_notional_per_run=self.config.max_notional_per_run,
                state_path=self.config.budget_state_path,
            )
        # Initialize budget metrics on startup
        self.sync_budget_metrics()

    def reset_run_counters(self) -> None:
        """Reset per-run counters. Call at start of each reconcile run."""
        self._orders_this_run = 0
        self._symbols_this_run = set()
        if self.budget_tracker:
            self.budget_tracker.reset_run_counters()
        # Sync budget state to metrics
        self.sync_budget_metrics()

    def sync_budget_metrics(self) -> None:
        """Push budget state to global ReconcileMetrics (LC-18).

        Should be called:
        - After __post_init__ (initialization)
        - After reset_run_counters() (each run start)
        - After record_execution() (each execution)

        If budget_tracker is None, sets budget_configured=False.
        """
        metrics = get_reconcile_metrics()
        if self.budget_tracker is None:
            metrics.set_budget_metrics(
                calls_used=0,
                notional_used=Decimal("0"),
                calls_remaining=0,
                notional_remaining=Decimal("0"),
                configured=False,
            )
            return

        used = self.budget_tracker.get_used()
        remaining = self.budget_tracker.get_remaining()
        metrics.set_budget_metrics(
            calls_used=int(used["calls_used_day"]),
            notional_used=Decimal(str(used["notional_used_day"])),
            calls_remaining=int(remaining["calls_remaining_day"]),
            notional_remaining=Decimal(str(remaining["notional_remaining_day"])),
            configured=True,
        )

    def _check_env_var(self) -> bool:
        """Check ALLOW_MAINNET_TRADE env var."""
        return os.environ.get("ALLOW_MAINNET_TRADE", "").lower() in ("1", "true", "yes")

    def _check_cooldown(self) -> bool:
        """Check if cooldown has elapsed since last action."""
        if self._last_action_ts == 0:
            return True
        now_ms = int(time.time() * 1000)
        elapsed_sec = (now_ms - self._last_action_ts) / 1000.0
        return elapsed_sec >= self.config.cooldown_seconds

    def _check_symbol_whitelist(self, symbol: str) -> bool:
        """Check if symbol is in whitelist."""
        if not self.symbol_whitelist:
            return False
        return symbol in self.symbol_whitelist

    def _check_max_orders(self) -> bool:
        """Check if under max orders per action limit."""
        return self._orders_this_run < self.config.max_orders_per_action

    def _check_max_symbols(self, symbol: str) -> bool:
        """Check if under max symbols per action limit."""
        if symbol in self._symbols_this_run:
            return True  # Already counting this symbol
        return len(self._symbols_this_run) < self.config.max_symbols_per_action

    def _check_mode_allows_action(  # noqa: PLR0911 - mode enum requires many returns
        self, is_cancel: bool
    ) -> tuple[bool, RemediationBlockReason | None]:
        """Check if the current mode allows this action type.

        LC-18: Mode-based gating for staged rollout.

        Args:
            is_cancel: True for cancel, False for flatten

        Returns:
            (allowed, block_reason): Tuple of result and optional reason
        """
        mode = self.config.remediation_mode

        # DETECT_ONLY: No planning or execution
        if mode == RemediationMode.DETECT_ONLY:
            return (False, RemediationBlockReason.MODE_DETECT_ONLY)

        # PLAN_ONLY: Planning allowed, execution blocked
        if mode == RemediationMode.PLAN_ONLY:
            return (False, RemediationBlockReason.MODE_PLAN_ONLY)

        # BLOCKED: Planning allowed, execution blocked (different metric label)
        if mode == RemediationMode.BLOCKED:
            return (False, RemediationBlockReason.MODE_BLOCKED)

        # EXECUTE_CANCEL_ALL: Only cancel_all allowed
        if mode == RemediationMode.EXECUTE_CANCEL_ALL:
            if not is_cancel:
                return (False, RemediationBlockReason.MODE_CANCEL_ONLY)
            return (True, None)

        # EXECUTE_FLATTEN: Only flatten allowed
        if mode == RemediationMode.EXECUTE_FLATTEN:
            if is_cancel:
                return (False, RemediationBlockReason.MODE_FLATTEN_ONLY)
            return (True, None)

        # Unknown mode - block
        return (False, RemediationBlockReason.MODE_BLOCKED)

    def _check_strategy_allowlist(self, client_order_id: str | None) -> bool:
        """Check if order's strategy is in remediation allowlist.

        LC-18: Strategy-based filtering for staged rollout.

        Args:
            client_order_id: Order ID to check

        Returns:
            True if strategy is allowed or no allowlist configured
        """
        # If no allowlist configured, allow all
        if not self.config.remediation_strategy_allowlist:
            return True

        # If no client_order_id, cannot check - deny
        if not client_order_id:
            return False

        # Parse order ID to extract strategy
        parsed = parse_client_order_id(client_order_id)
        if parsed is None:
            return False

        return parsed.strategy_id in self.config.remediation_strategy_allowlist

    def _check_remediation_symbol_allowlist(self, symbol: str) -> bool:
        """Check if symbol is in remediation allowlist.

        LC-18: Symbol-based filtering for staged rollout.

        Args:
            symbol: Trading symbol to check

        Returns:
            True if symbol is allowed or no allowlist configured
        """
        # If no allowlist configured, allow all
        if not self.config.remediation_symbol_allowlist:
            return True

        return symbol in self.config.remediation_symbol_allowlist

    def can_execute(  # noqa: PLR0911, PLR0912 - many gates require many returns/branches
        self,
        symbol: str,
        is_cancel: bool,
        client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
    ) -> tuple[bool, RemediationBlockReason | None]:
        """Check if remediation can execute (LC-10 gates + LC-18 extensions).

        Args:
            symbol: Trading symbol
            is_cancel: True for cancel, False for flatten
            client_order_id: Order ID (for cancel prefix check)
            notional_usdt: Position notional (for flatten limit check)

        Returns:
            (can_execute, block_reason): Tuple of result and optional reason

        Gate sequence (LC-18 additions first):
            0a. Mode allows this action type
            0b. Budget limits (per-run and per-day)
            0c. Strategy in allowlist (if configured)
            0d. Symbol in remediation allowlist (if configured)

            1. action != NONE (legacy)
            2. dry_run == False (legacy)
            3. allow_active_remediation == True (legacy)
            4. armed == True
            5. ALLOW_MAINNET_TRADE env var set
            6. cooldown elapsed
            7. symbol in whitelist (or whitelist not required)
            8. grinder_ prefix (cancel only)
            9. notional <= limit (flatten only)

        Additional limits:
            - max_orders_per_action
            - max_symbols_per_action
            - require_whitelist
        """
        # LC-18 Gate 0a: Mode check
        mode_ok, mode_reason = self._check_mode_allows_action(is_cancel)
        if not mode_ok:
            return (False, mode_reason)

        # LC-18 Gate 0b: Budget check
        if self.budget_tracker:
            budget_notional = notional_usdt or Decimal("0")
            budget_ok, budget_reason = self.budget_tracker.can_execute(budget_notional)
            if not budget_ok:
                # Map budget reason string to enum
                reason_map = {
                    "max_calls_per_run": RemediationBlockReason.MAX_CALLS_PER_RUN,
                    "max_notional_per_run": RemediationBlockReason.MAX_NOTIONAL_PER_RUN,
                    "max_calls_per_day": RemediationBlockReason.MAX_CALLS_PER_DAY,
                    "max_notional_per_day": RemediationBlockReason.MAX_NOTIONAL_PER_DAY,
                }
                return (
                    False,
                    reason_map.get(budget_reason or "", RemediationBlockReason.MAX_CALLS_PER_DAY),
                )

        # LC-18 Gate 0c: Strategy allowlist check (cancel only)
        if is_cancel and not self._check_strategy_allowlist(client_order_id):
            return (False, RemediationBlockReason.STRATEGY_NOT_ALLOWED)

        # LC-18 Gate 0d: Symbol remediation allowlist check
        if not self._check_remediation_symbol_allowlist(symbol):
            return (False, RemediationBlockReason.SYMBOL_NOT_IN_REMEDIATION_ALLOWLIST)

        # Gate 1: action != NONE (legacy - for backward compat)
        if self.config.action == RemediationAction.NONE:
            return (False, RemediationBlockReason.ACTION_IS_NONE)

        # Gate 2: dry_run == False (legacy - for backward compat)
        if self.config.dry_run:
            return (False, RemediationBlockReason.DRY_RUN)

        # Gate 3: allow_active_remediation == True (legacy - for backward compat)
        if not self.config.allow_active_remediation:
            return (False, RemediationBlockReason.NOT_ALLOWED)

        # Gate 4: armed == True
        if not self.armed:
            return (False, RemediationBlockReason.NOT_ARMED)

        # Gate 5: ALLOW_MAINNET_TRADE env var
        if not self._check_env_var():
            return (False, RemediationBlockReason.ENV_VAR_MISSING)

        # Gate 6: cooldown elapsed
        if not self._check_cooldown():
            return (False, RemediationBlockReason.COOLDOWN_NOT_ELAPSED)

        # Whitelist requirement check
        if self.config.require_whitelist and not self.symbol_whitelist:
            return (False, RemediationBlockReason.WHITELIST_REQUIRED)

        # Gate 7: symbol in whitelist
        if self.symbol_whitelist and not self._check_symbol_whitelist(symbol):
            return (False, RemediationBlockReason.SYMBOL_NOT_IN_WHITELIST)

        # Gate 8: Order identity check (cancel only)
        # LC-12: Check prefix + strategy allowlist, not just hardcoded prefix
        if is_cancel and client_order_id:
            identity = self.identity_config or get_default_identity_config()
            if not is_ours(client_order_id, identity):
                return (False, RemediationBlockReason.NO_GRINDER_PREFIX)

        # Gate 9: notional <= limit (flatten only)
        if (
            not is_cancel
            and notional_usdt is not None
            and notional_usdt > self.config.flatten_max_notional_per_call
        ):
            return (False, RemediationBlockReason.NOTIONAL_EXCEEDS_LIMIT)

        # Additional limits (legacy)
        if not self._check_max_orders():
            return (False, RemediationBlockReason.MAX_ORDERS_REACHED)

        if not self._check_max_symbols(symbol):
            return (False, RemediationBlockReason.MAX_SYMBOLS_REACHED)

        return (True, None)

    def remediate_cancel(self, observed_order: ObservedOrder) -> RemediationResult:
        """Remediate an unexpected order by cancelling it.

        Args:
            observed_order: The unexpected order to cancel

        Returns:
            RemediationResult with status and details
        """
        symbol = observed_order.symbol
        client_order_id = observed_order.client_order_id
        action = "cancel_all"
        mismatch_type = MismatchType.ORDER_EXISTS_UNEXPECTED.value
        metrics = get_reconcile_metrics()

        # Check gates
        can_exec, block_reason = self.can_execute(
            symbol=symbol,
            is_cancel=True,
            client_order_id=client_order_id,
        )

        if not can_exec:
            # LC-18: Determine if this is a planning scenario or a real block
            # DETECT_ONLY: No planning at all
            # PLAN_ONLY, MODE_BLOCKED, DRY_RUN, ACTION_IS_NONE: Record as planned
            # Everything else: Record as blocked
            planning_reasons = (
                RemediationBlockReason.ACTION_IS_NONE,
                RemediationBlockReason.DRY_RUN,
                RemediationBlockReason.MODE_PLAN_ONLY,
                RemediationBlockReason.MODE_BLOCKED,
            )
            detect_only_reasons = (RemediationBlockReason.MODE_DETECT_ONLY,)

            if block_reason in detect_only_reasons:
                # DETECT_ONLY: No metrics, no logging (pure detection)
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    status=RemediationStatus.PLANNED,
                    block_reason=block_reason,
                    action=action,
                )
            elif block_reason in planning_reasons:
                # Planning mode: record as planned
                metrics.record_action_planned(action)
                logger.info(
                    "REMEDIATION_PLANNED",
                    extra={
                        "action": action,
                        "symbol": symbol,
                        "client_order_id": client_order_id,
                        "reason": block_reason.value if block_reason else "none",
                    },
                )
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    status=RemediationStatus.PLANNED,
                    block_reason=block_reason,
                    action=action,
                )
            else:
                # Real block: record as blocked
                metrics.record_action_blocked(block_reason.value if block_reason else "unknown")
                logger.warning(
                    "REMEDIATION_BLOCKED",
                    extra={
                        "action": action,
                        "symbol": symbol,
                        "client_order_id": client_order_id,
                        "reason": block_reason.value if block_reason else "unknown",
                    },
                )
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    status=RemediationStatus.BLOCKED,
                    block_reason=block_reason,
                    action=action,
                )

        # Execute cancel
        try:
            success = self.port.cancel_order(client_order_id)
            if success:
                # Update counters
                self._orders_this_run += 1
                self._symbols_this_run.add(symbol)
                self._last_action_ts = int(time.time() * 1000)

                # LC-18: Record budget usage (cancel has no notional)
                if self.budget_tracker:
                    self.budget_tracker.record_execution(Decimal("0"))
                    self.sync_budget_metrics()

                metrics.record_action_executed(action)
                logger.info(
                    "REMEDIATION_EXECUTED",
                    extra={
                        "action": action,
                        "symbol": symbol,
                        "client_order_id": client_order_id,
                    },
                )
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    status=RemediationStatus.EXECUTED,
                    action=action,
                )
            else:
                # Port returned False (unusual)
                metrics.record_action_blocked(RemediationBlockReason.PORT_ERROR.value)
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    status=RemediationStatus.FAILED,
                    block_reason=RemediationBlockReason.PORT_ERROR,
                    error="cancel_order returned False",
                    action=action,
                )
        except Exception as e:
            metrics.record_action_blocked(RemediationBlockReason.PORT_ERROR.value)
            logger.error(
                "REMEDIATION_FAILED",
                extra={
                    "action": action,
                    "symbol": symbol,
                    "client_order_id": client_order_id,
                    "error": str(e),
                },
            )
            return RemediationResult(
                mismatch_type=mismatch_type,
                symbol=symbol,
                client_order_id=client_order_id,
                status=RemediationStatus.FAILED,
                block_reason=RemediationBlockReason.PORT_ERROR,
                error=str(e),
                action=action,
            )

    def remediate_flatten(
        self,
        observed_position: ObservedPosition,
        current_price: Decimal,
    ) -> RemediationResult:
        """Remediate an unexpected position by flattening it.

        Args:
            observed_position: The unexpected position to flatten
            current_price: Current market price for notional calculation

        Returns:
            RemediationResult with status and details
        """
        symbol = observed_position.symbol
        position_amt = observed_position.position_amt
        action = "flatten"
        mismatch_type = MismatchType.POSITION_NONZERO_UNEXPECTED.value
        metrics = get_reconcile_metrics()

        # Calculate notional
        notional_usdt = abs(position_amt) * current_price

        # Check gates
        can_exec, block_reason = self.can_execute(
            symbol=symbol,
            is_cancel=False,
            notional_usdt=notional_usdt,
        )

        if not can_exec:
            # LC-18: Determine if this is a planning scenario or a real block
            planning_reasons = (
                RemediationBlockReason.ACTION_IS_NONE,
                RemediationBlockReason.DRY_RUN,
                RemediationBlockReason.MODE_PLAN_ONLY,
                RemediationBlockReason.MODE_BLOCKED,
            )
            detect_only_reasons = (RemediationBlockReason.MODE_DETECT_ONLY,)

            if block_reason in detect_only_reasons:
                # DETECT_ONLY: No metrics, no logging (pure detection)
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=None,
                    status=RemediationStatus.PLANNED,
                    block_reason=block_reason,
                    action=action,
                )
            elif block_reason in planning_reasons:
                # Planning mode: record as planned
                metrics.record_action_planned(action)
                logger.info(
                    "REMEDIATION_PLANNED",
                    extra={
                        "action": action,
                        "symbol": symbol,
                        "position_amt": str(position_amt),
                        "notional_usdt": str(notional_usdt),
                        "reason": block_reason.value if block_reason else "none",
                    },
                )
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=None,
                    status=RemediationStatus.PLANNED,
                    block_reason=block_reason,
                    action=action,
                )
            else:
                # Real block: record as blocked
                metrics.record_action_blocked(block_reason.value if block_reason else "unknown")
                logger.warning(
                    "REMEDIATION_BLOCKED",
                    extra={
                        "action": action,
                        "symbol": symbol,
                        "position_amt": str(position_amt),
                        "notional_usdt": str(notional_usdt),
                        "reason": block_reason.value if block_reason else "unknown",
                    },
                )
                return RemediationResult(
                    mismatch_type=mismatch_type,
                    symbol=symbol,
                    client_order_id=None,
                    status=RemediationStatus.BLOCKED,
                    block_reason=block_reason,
                    action=action,
                )

        # Execute flatten via market order with reduceOnly
        try:
            # Determine close side (opposite of position)
            # Positive position_amt = long → need to SELL
            # Negative position_amt = short → need to BUY
            close_side = OrderSide.SELL if position_amt > 0 else OrderSide.BUY
            close_qty = abs(position_amt)

            order_id = self.port.place_market_order(
                symbol=symbol,
                side=close_side,
                quantity=close_qty,
                reduce_only=True,
            )

            # Update counters
            self._orders_this_run += 1
            self._symbols_this_run.add(symbol)
            self._last_action_ts = int(time.time() * 1000)

            # LC-18: Record budget usage with notional
            if self.budget_tracker:
                self.budget_tracker.record_execution(notional_usdt)
                self.sync_budget_metrics()

            metrics.record_action_executed(action)
            logger.info(
                "REMEDIATION_EXECUTED",
                extra={
                    "action": action,
                    "symbol": symbol,
                    "position_amt": str(position_amt),
                    "close_side": close_side.value,
                    "close_qty": str(close_qty),
                    "order_id": order_id,
                },
            )
            return RemediationResult(
                mismatch_type=mismatch_type,
                symbol=symbol,
                client_order_id=order_id,
                status=RemediationStatus.EXECUTED,
                action=action,
            )
        except Exception as e:
            metrics.record_action_blocked(RemediationBlockReason.PORT_ERROR.value)
            logger.error(
                "REMEDIATION_FAILED",
                extra={
                    "action": action,
                    "symbol": symbol,
                    "position_amt": str(position_amt),
                    "error": str(e),
                },
            )
            return RemediationResult(
                mismatch_type=mismatch_type,
                symbol=symbol,
                client_order_id=None,
                status=RemediationStatus.FAILED,
                block_reason=RemediationBlockReason.PORT_ERROR,
                error=str(e),
                action=action,
            )
