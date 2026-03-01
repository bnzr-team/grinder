"""Drawdown guard v1 for portfolio and symbol-level risk management.

This module provides a deterministic drawdown guard that:
- Tracks DD at portfolio level AND per-symbol level
- Transitions to DRAWDOWN state when limits are breached
- Blocks risk-increasing orders while allowing reduce-only
- No auto-recovery (manual reset only for determinism)

Key invariants (ADR-033):
    1. State latching: DRAWDOWN state persists until explicit reset
    2. Intent classification: deterministic increase vs reduce logic
    3. No flapping: no auto-recovery to prevent indeterminate states
    4. Reason codes: low-cardinality stable strings

Usage:
    guard = DrawdownGuardV1(
        portfolio_dd_limit=Decimal("0.20"),  # 20%
        symbol_dd_budgets={"BTCUSDT": Decimal("1000"), "ETHUSDT": Decimal("500")},
    )

    # Update on each tick
    guard.update(
        equity_current=Decimal("95000"),
        equity_start=Decimal("100000"),
        symbol_losses={"BTCUSDT": Decimal("800")},
    )

    # Check before placing order
    decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
    if not decision.allowed:
        logger.warning("Blocked: %s", decision.reason)

See: ADR-033 for design decisions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class GuardError(Exception):
    """Non-retryable error in drawdown guard.

    Raised when inputs are invalid or configuration is incorrect.
    """

    pass


class GuardState(Enum):
    """State of the drawdown guard.

    NORMAL: Trading allowed, all intents permitted
    DRAWDOWN: DD limit breached, only reduce-risk intents allowed
    """

    NORMAL = "NORMAL"
    DRAWDOWN = "DRAWDOWN"


class OrderIntent(Enum):
    """Classification of order intent for risk evaluation.

    INCREASE_RISK: Orders that increase exposure (new positions, grid entries)
    REDUCE_RISK: Orders that decrease exposure (closes, reduce-only)
    CANCEL: Cancellation of existing orders (always allowed)
    """

    INCREASE_RISK = "INCREASE_RISK"
    REDUCE_RISK = "REDUCE_RISK"
    CANCEL = "CANCEL"


class AllowReason(Enum):
    """Reason codes for allow/block decisions.

    Low-cardinality stable strings for metrics/logging.
    """

    # Allowed reasons
    NORMAL_STATE = "NORMAL_STATE"
    REDUCE_RISK_ALLOWED = "REDUCE_RISK_ALLOWED"
    CANCEL_ALWAYS_ALLOWED = "CANCEL_ALWAYS_ALLOWED"

    # Blocked reasons
    DD_PORTFOLIO_BREACH = "DD_PORTFOLIO_BREACH"
    DD_SYMBOL_BREACH = "DD_SYMBOL_BREACH"


@dataclass(frozen=True)
class AllowDecision:
    """Result of guard's allow() check.

    Attributes:
        allowed: Whether the intent is permitted
        reason: Low-cardinality reason code
        state: Current guard state
        details: Additional context (for debugging/logging)
    """

    allowed: bool
    reason: AllowReason
    state: GuardState
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "allowed": self.allowed,
            "reason": self.reason.value,
            "state": self.state.value,
            "details": self.details,
        }


@dataclass(frozen=True)
class GuardSnapshot:
    """Snapshot of guard state at a point in time.

    Useful for observability and debugging.
    """

    state: GuardState
    portfolio_dd_pct: Decimal  # Current portfolio DD as fraction (0.20 = 20%)
    portfolio_dd_limit: Decimal
    symbol_losses: dict[str, Decimal]  # symbol -> loss in USD
    symbol_dd_budgets: dict[str, Decimal]  # symbol -> budget in USD
    breached_symbols: list[str]  # Symbols that breached their limit
    trigger_reason: AllowReason | None  # What caused DRAWDOWN state

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "state": self.state.value,
            "portfolio_dd_pct": str(self.portfolio_dd_pct),
            "portfolio_dd_limit": str(self.portfolio_dd_limit),
            "symbol_losses": {k: str(v) for k, v in self.symbol_losses.items()},
            "symbol_dd_budgets": {k: str(v) for k, v in self.symbol_dd_budgets.items()},
            "breached_symbols": self.breached_symbols,
            "trigger_reason": self.trigger_reason.value if self.trigger_reason else None,
        }


@dataclass
class DrawdownGuardV1Config:
    """Configuration for DrawdownGuardV1.

    Attributes:
        portfolio_dd_limit: Maximum portfolio DD as fraction (e.g., 0.20 = 20%)
        symbol_dd_budgets: Per-symbol DD budget in USD
    """

    portfolio_dd_limit: Decimal = field(default_factory=lambda: Decimal("0.20"))
    symbol_dd_budgets: dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.portfolio_dd_limit <= 0:
            raise GuardError(f"portfolio_dd_limit must be > 0, got {self.portfolio_dd_limit}")
        if self.portfolio_dd_limit > 1:
            raise GuardError(f"portfolio_dd_limit must be <= 1.0, got {self.portfolio_dd_limit}")
        for symbol, budget in self.symbol_dd_budgets.items():
            if budget < 0:
                raise GuardError(f"symbol_dd_budget for {symbol} must be >= 0, got {budget}")


class DrawdownGuardV1:
    """Drawdown guard v1 with portfolio and per-symbol limits.

    Monitors drawdown at two levels:
    1. Portfolio: (equity_start - equity_current) / equity_start >= limit
    2. Symbol: symbol_loss >= symbol_dd_budget

    When either limit is breached, transitions to DRAWDOWN state:
    - INCREASE_RISK intents are blocked
    - REDUCE_RISK intents are allowed
    - CANCEL intents are always allowed

    State is latched: no auto-recovery. Only explicit reset() returns to NORMAL.
    This ensures deterministic behavior across replay runs.

    Thread safety: Not thread-safe. Use one instance per engine.
    """

    def __init__(self, config: DrawdownGuardV1Config | None = None) -> None:
        """Initialize drawdown guard.

        Args:
            config: Guard configuration (uses defaults if None)
        """
        self._config = config or DrawdownGuardV1Config()
        self._state = GuardState.NORMAL
        self._trigger_reason: AllowReason | None = None
        self._portfolio_dd_pct = Decimal("0")
        self._symbol_losses: dict[str, Decimal] = {}
        self._breached_symbols: list[str] = []

    @property
    def config(self) -> DrawdownGuardV1Config:
        """Get current configuration."""
        return self._config

    @property
    def state(self) -> GuardState:
        """Get current guard state."""
        return self._state

    @property
    def is_drawdown(self) -> bool:
        """Check if guard is in DRAWDOWN state."""
        return self._state == GuardState.DRAWDOWN

    @property
    def current_drawdown_pct(self) -> float:
        """Current portfolio drawdown as fraction (0.20 = 20%).

        Returns 0.0 if no update has been called yet.
        """
        return float(self._portfolio_dd_pct)

    def update(
        self,
        *,
        equity_current: Decimal,
        equity_start: Decimal,
        symbol_losses: dict[str, Decimal] | None = None,
    ) -> GuardSnapshot:
        """Update guard state with current values.

        Checks both portfolio and symbol-level limits.
        Transitions to DRAWDOWN if any limit is breached.
        Once in DRAWDOWN, stays there (latched) until reset().

        Args:
            equity_current: Current portfolio equity
            equity_start: Starting equity (session/day start)
            symbol_losses: Per-symbol realized losses in USD (positive = loss)

        Returns:
            GuardSnapshot with current state

        Raises:
            GuardError: If inputs are invalid
        """
        # Validate inputs
        if equity_start <= 0:
            raise GuardError(f"equity_start must be > 0, got {equity_start}")
        if equity_current < 0:
            raise GuardError(f"equity_current must be >= 0, got {equity_current}")

        symbol_losses = symbol_losses or {}
        for symbol, loss in symbol_losses.items():
            if loss < 0:
                raise GuardError(f"symbol_loss for {symbol} must be >= 0 (loss), got {loss}")

        # Store for snapshot
        self._symbol_losses = dict(symbol_losses)

        # Compute portfolio DD
        self._portfolio_dd_pct = (equity_start - equity_current) / equity_start
        # Clamp to non-negative (equity above start = 0% DD)
        if self._portfolio_dd_pct < 0:
            self._portfolio_dd_pct = Decimal("0")

        # If already in DRAWDOWN, stay there (latched)
        if self._state == GuardState.DRAWDOWN:
            return self._create_snapshot()

        # Check portfolio-level breach
        if self._portfolio_dd_pct >= self._config.portfolio_dd_limit:
            self._state = GuardState.DRAWDOWN
            self._trigger_reason = AllowReason.DD_PORTFOLIO_BREACH
            logger.warning(
                "DrawdownGuardV1: DRAWDOWN triggered (portfolio DD %.2f%% >= %.2f%%)",
                float(self._portfolio_dd_pct * 100),
                float(self._config.portfolio_dd_limit * 100),
            )
            return self._create_snapshot()

        # Check symbol-level breaches
        self._breached_symbols = []
        for symbol, loss in symbol_losses.items():
            budget = self._config.symbol_dd_budgets.get(symbol)
            if budget is not None and loss >= budget:
                self._breached_symbols.append(symbol)

        if self._breached_symbols:
            self._state = GuardState.DRAWDOWN
            self._trigger_reason = AllowReason.DD_SYMBOL_BREACH
            logger.warning(
                "DrawdownGuardV1: DRAWDOWN triggered (symbols breached: %s)",
                self._breached_symbols,
            )
            return self._create_snapshot()

        return self._create_snapshot()

    def allow(
        self,
        intent: OrderIntent,
        symbol: str | None = None,
    ) -> AllowDecision:
        """Check if an order intent is allowed.

        Decision logic:
        - NORMAL state: all intents allowed
        - DRAWDOWN state:
            - CANCEL: always allowed
            - REDUCE_RISK: allowed (decreases exposure)
            - INCREASE_RISK: blocked

        Args:
            intent: Type of order intent
            symbol: Symbol for the intent (optional, for logging)

        Returns:
            AllowDecision with allowed/blocked status and reason
        """
        # CANCEL is always allowed
        if intent == OrderIntent.CANCEL:
            return AllowDecision(
                allowed=True,
                reason=AllowReason.CANCEL_ALWAYS_ALLOWED,
                state=self._state,
            )

        # In NORMAL state, everything is allowed
        if self._state == GuardState.NORMAL:
            return AllowDecision(
                allowed=True,
                reason=AllowReason.NORMAL_STATE,
                state=self._state,
            )

        # In DRAWDOWN state
        if intent == OrderIntent.REDUCE_RISK:
            return AllowDecision(
                allowed=True,
                reason=AllowReason.REDUCE_RISK_ALLOWED,
                state=self._state,
            )

        # INCREASE_RISK in DRAWDOWN -> blocked
        details: dict[str, Any] = {
            "portfolio_dd_pct": str(self._portfolio_dd_pct),
            "portfolio_dd_limit": str(self._config.portfolio_dd_limit),
        }
        if symbol:
            details["symbol"] = symbol
            if symbol in self._breached_symbols:
                details["symbol_breached"] = True
                budget = self._config.symbol_dd_budgets.get(symbol)
                loss = self._symbol_losses.get(symbol, Decimal("0"))
                details["symbol_budget"] = str(budget) if budget else None
                details["symbol_loss"] = str(loss)

        # Use the trigger reason for the block reason
        block_reason = self._trigger_reason or AllowReason.DD_PORTFOLIO_BREACH

        return AllowDecision(
            allowed=False,
            reason=block_reason,
            state=self._state,
            details=details,
        )

    def reset(self) -> None:
        """Reset guard to NORMAL state.

        Should only be called for new session/day start.
        This is the only way to exit DRAWDOWN state (no auto-recovery).
        """
        self._state = GuardState.NORMAL
        self._trigger_reason = None
        self._portfolio_dd_pct = Decimal("0")
        self._symbol_losses = {}
        self._breached_symbols = []
        logger.info("DrawdownGuardV1: Reset to NORMAL state")

    def snapshot(self) -> GuardSnapshot:
        """Get current guard state snapshot."""
        return self._create_snapshot()

    def _create_snapshot(self) -> GuardSnapshot:
        """Create snapshot of current state."""
        return GuardSnapshot(
            state=self._state,
            portfolio_dd_pct=self._portfolio_dd_pct,
            portfolio_dd_limit=self._config.portfolio_dd_limit,
            symbol_losses=dict(self._symbol_losses),
            symbol_dd_budgets=dict(self._config.symbol_dd_budgets),
            breached_symbols=list(self._breached_symbols),
            trigger_reason=self._trigger_reason,
        )
