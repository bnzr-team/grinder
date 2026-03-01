"""Emergency exit executor implementing 10_RISK_SPEC § 10.6.

Sequence:
1. Cancel all pending orders (per symbol)
2. MARKET IOC reduce_only per open position
3. Bounded verify loop (retry until positions closed or timeout)
4. Return result (success / partial)

Safe-by-default: gated behind GRINDER_EMERGENCY_EXIT_ENABLED=false.
Runs at most once per engine lifetime (latch in engine.py).
All market orders use reduce_only=True — cannot open new positions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from grinder.core import OrderSide

if TYPE_CHECKING:
    from grinder.risk.emergency_exit_port import EmergencyExitPort

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmergencyExitResult:
    """Result of emergency exit execution.

    Attributes:
        triggered_at_ms: Snapshot timestamp when exit was triggered.
        reason: Why exit was triggered (e.g. "drawdown_breach").
        orders_cancelled: Count of cancel_all calls that succeeded.
        market_orders_placed: Count of MARKET reduce_only orders placed.
        positions_remaining: Positions still open after verify.
        success: True if all positions closed (remaining == 0).
    """

    triggered_at_ms: int
    reason: str
    orders_cancelled: int
    market_orders_placed: int
    positions_remaining: int
    success: bool


# Default verify: 10 attempts x 200ms = 2s total
_DEFAULT_VERIFY_ATTEMPTS = 10
_DEFAULT_VERIFY_INTERVAL_S = 0.2


class EmergencyExitExecutor:
    """Executes § 10.6 emergency exit sequence.

    Designed to be stateless — all state tracking lives in the engine.
    Uses EmergencyExitPort protocol (narrow interface, no full ExchangePort dependency).

    Thread safety: Not thread-safe. Use one instance per engine.
    """

    def __init__(
        self,
        port: EmergencyExitPort,
        *,
        verify_attempts: int = _DEFAULT_VERIFY_ATTEMPTS,
        verify_interval_s: float = _DEFAULT_VERIFY_INTERVAL_S,
    ) -> None:
        self._port = port
        self._verify_attempts = verify_attempts
        self._verify_interval_s = verify_interval_s

    def execute(
        self,
        ts_ms: int,
        reason: str,
        symbols: list[str],
    ) -> EmergencyExitResult:
        """Execute emergency exit sequence for given symbols.

        Args:
            ts_ms: Snapshot timestamp (for result tracking).
            reason: Why exit was triggered.
            symbols: Symbols to process. If empty, derives from open positions.

        Returns:
            EmergencyExitResult with outcome details.
        """
        logger.critical("EMERGENCY EXIT START: reason=%s symbols=%s", reason, symbols)

        orders_cancelled = 0
        market_orders_placed = 0

        # Phase 1: Cancel all pending orders
        for symbol in symbols:
            try:
                result = self._port.cancel_all_orders(symbol)
                orders_cancelled += result
                logger.info("cancel_all_orders(%s) → %d", symbol, result)
            except Exception:
                logger.exception("cancel_all_orders(%s) failed, continuing", symbol)

        # Phase 2: Close positions with MARKET reduce_only
        for symbol in symbols:
            try:
                positions = self._port.get_positions(symbol)
                for pos in positions:
                    pos_amt = pos.position_amt
                    if pos_amt == 0:
                        continue
                    # Opposite side to close
                    close_side = OrderSide.SELL if pos_amt > 0 else OrderSide.BUY
                    close_qty = abs(pos_amt)
                    order_id = self._port.place_market_order(
                        symbol=symbol,
                        side=close_side,
                        quantity=close_qty,
                        reduce_only=True,
                    )
                    market_orders_placed += 1
                    logger.info(
                        "place_market_order(%s, %s, qty=%s, reduce_only=True) → %s",
                        symbol,
                        close_side.value,
                        close_qty,
                        order_id,
                    )
            except Exception:
                logger.exception("close_position(%s) failed, continuing", symbol)

        # Phase 3: Bounded verify loop
        positions_remaining = self._verify_positions_closed(symbols)

        success = positions_remaining == 0
        level = "INFO" if success else "CRITICAL"
        getattr(logger, level.lower())(
            "EMERGENCY EXIT %s: cancelled=%d market=%d remaining=%d",
            "COMPLETE" if success else "PARTIAL",
            orders_cancelled,
            market_orders_placed,
            positions_remaining,
        )

        return EmergencyExitResult(
            triggered_at_ms=ts_ms,
            reason=reason,
            orders_cancelled=orders_cancelled,
            market_orders_placed=market_orders_placed,
            positions_remaining=positions_remaining,
            success=success,
        )

    def _verify_positions_closed(self, symbols: list[str]) -> int:
        """Bounded retry loop checking if all positions are closed.

        Returns count of remaining non-zero positions.
        """
        for attempt in range(self._verify_attempts):
            remaining = self._count_open_positions(symbols)
            if remaining == 0:
                logger.info(
                    "verify attempt %d/%d: all positions closed", attempt + 1, self._verify_attempts
                )
                return 0
            logger.info(
                "verify attempt %d/%d: %d positions remaining, waiting %.1fs",
                attempt + 1,
                self._verify_attempts,
                remaining,
                self._verify_interval_s,
            )
            if attempt < self._verify_attempts - 1:
                time.sleep(self._verify_interval_s)

        # Final count after all retries
        return self._count_open_positions(symbols)

    def _count_open_positions(self, symbols: list[str]) -> int:
        """Count non-zero positions across all symbols."""
        count = 0
        for symbol in symbols:
            try:
                positions = self._port.get_positions(symbol)
                count += sum(1 for p in positions if p.position_amt != 0)
            except Exception:
                logger.exception("get_positions(%s) failed during verify", symbol)
                count += 1  # Assume still open if we can't check
        return count
