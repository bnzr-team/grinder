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
from enum import Enum
from typing import TYPE_CHECKING

from grinder.core import OrderSide
from grinder.reconcile.config import ReconcileConfig, RemediationAction
from grinder.reconcile.identity import (
    OrderIdentityConfig,
    get_default_identity_config,
    is_ours,
)
from grinder.reconcile.metrics import get_reconcile_metrics
from grinder.reconcile.types import MismatchType, ObservedOrder, ObservedPosition

if TYPE_CHECKING:
    from decimal import Decimal

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

    Safety architecture:
    - 9 gates must ALL pass for real execution
    - Kill-switch ALLOWS remediation (reduces risk exposure)
    - Default: dry-run only

    Thread-safety: No (use separate instances per thread)
    """

    config: ReconcileConfig
    port: BinanceFuturesPort
    armed: bool = False
    symbol_whitelist: list[str] = field(default_factory=list)
    kill_switch_active: bool = False
    identity_config: OrderIdentityConfig | None = None

    # Internal state
    _last_action_ts: int = field(default=0, repr=False)
    _orders_this_run: int = field(default=0, repr=False)
    _symbols_this_run: set[str] = field(default_factory=set, repr=False)

    def reset_run_counters(self) -> None:
        """Reset per-run counters. Call at start of each reconcile run."""
        self._orders_this_run = 0
        self._symbols_this_run = set()

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

    def can_execute(  # noqa: PLR0911, PLR0912 - 9 gates require many returns/branches
        self,
        symbol: str,
        is_cancel: bool,
        client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
    ) -> tuple[bool, RemediationBlockReason | None]:
        """Check if remediation can execute (all 9 gates).

        Args:
            symbol: Trading symbol
            is_cancel: True for cancel, False for flatten
            client_order_id: Order ID (for cancel prefix check)
            notional_usdt: Position notional (for flatten limit check)

        Returns:
            (can_execute, block_reason): Tuple of result and optional reason

        Gate sequence:
            1. action != NONE
            2. dry_run == False
            3. allow_active_remediation == True
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
        # Gate 1: action != NONE
        if self.config.action == RemediationAction.NONE:
            return (False, RemediationBlockReason.ACTION_IS_NONE)

        # Gate 2: dry_run == False
        if self.config.dry_run:
            return (False, RemediationBlockReason.DRY_RUN)

        # Gate 3: allow_active_remediation == True
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
            and notional_usdt > self.config.max_flatten_notional_usdt
        ):
            return (False, RemediationBlockReason.NOTIONAL_EXCEEDS_LIMIT)

        # Additional limits
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
            # Determine if this is a dry-run plan or a real block
            if block_reason in (
                RemediationBlockReason.ACTION_IS_NONE,
                RemediationBlockReason.DRY_RUN,
            ):
                # Dry-run: record as planned
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
            # Determine if this is a dry-run plan or a real block
            if block_reason in (
                RemediationBlockReason.ACTION_IS_NONE,
                RemediationBlockReason.DRY_RUN,
            ):
                # Dry-run: record as planned
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
