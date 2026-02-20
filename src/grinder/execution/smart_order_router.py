"""SmartOrderRouter — pure decision logic for amend vs cancel-replace.

Launch-14 PR1: Pure function `route(inputs) -> RouteResult`.
SSOT: docs/14_SMART_ORDER_ROUTER_SPEC.md (decision matrix + invariants).

Design:
- Zero I/O, zero logging, zero metrics (caller-side only in PR2).
- All inputs via frozen dataclasses; output is frozen dataclass.
- Deterministic: same RouterInputs always produces identical RouteResult.
- Decision priority (first match wins):
  1. Hard BLOCKs (spread crossing, filter violations)
  2. Budget rules (rate-limit throttle)
  3. NOOP epsilon (no meaningful change)
  4. Prefer AMEND (when supported and safe)
  5. Fallback CANCEL_REPLACE
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRICE_EPS_TICKS_DEFAULT = 1
QTY_EPS_STEPS_DEFAULT = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RouterDecision(StrEnum):
    """Router decision outcome (SSOT §14.4)."""

    NOOP = "NOOP"
    AMEND = "AMEND"
    CANCEL_REPLACE = "CANCEL_REPLACE"
    BLOCK = "BLOCK"


# ---------------------------------------------------------------------------
# Input dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingOrder:
    """Snapshot of the currently open order on this level.

    Attributes:
        order_id: Exchange-assigned or client order ID.
        price: Current limit price.
        qty: Current order quantity.
        side: "BUY" or "SELL".
        reduce_only: Whether the order is reduce-only.
        time_in_force: TIF string (e.g. "GTC", "IOC", "GTX").
    """

    order_id: str
    price: Decimal
    qty: Decimal
    side: str
    reduce_only: bool = False
    time_in_force: str = "GTC"


@dataclass(frozen=True)
class OrderIntent:
    """Desired target for this grid level.

    Attributes:
        price: Target limit price.
        qty: Target quantity.
        side: "BUY" or "SELL".
        reduce_only: Whether the order should be reduce-only.
        time_in_force: TIF string (e.g. "GTC", "IOC", "GTX").
    """

    price: Decimal
    qty: Decimal
    side: str
    reduce_only: bool = False
    time_in_force: str = "GTC"


@dataclass(frozen=True)
class MarketSnapshot:
    """Minimal market state needed for spread-crossing check.

    Attributes:
        best_bid: Current best bid price.
        best_ask: Current best ask price.
    """

    best_bid: Decimal
    best_ask: Decimal


@dataclass(frozen=True)
class ExchangeFilters:
    """Exchange symbol filters for constraint validation (SSOT §14.7).

    Attributes:
        tick_size: Minimum price increment (PRICE_FILTER).
        step_size: Lot size step for qty rounding (LOT_SIZE).
        min_qty: Minimum order quantity (LOT_SIZE).
        min_notional: Minimum order value (MIN_NOTIONAL).
    """

    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class VenueCaps:
    """Venue capabilities for amend support (SSOT invariant I6).

    Attributes:
        supports_amend_price: Whether the venue supports price amendment.
        supports_amend_qty: Whether the venue supports quantity amendment.
    """

    supports_amend_price: bool = True
    supports_amend_qty: bool = True


@dataclass(frozen=True)
class UpdateBudgets:
    """Rate-limit budgets for this cycle (SSOT §14.5 budget rules).

    Attributes:
        updates_remaining: Total API calls remaining in budget window.
        cancel_replace_remaining: Cancel+place pairs remaining (each costs 2).
    """

    updates_remaining: int = 100
    cancel_replace_remaining: int = 50


@dataclass(frozen=True)
class RouterInputs:
    """All inputs for a single route() call.

    Gathers intent, existing order, market state, filters, venue capabilities,
    budgets, and drawdown state into one frozen bundle.
    """

    intent: OrderIntent
    existing: ExistingOrder | None
    market: MarketSnapshot
    filters: ExchangeFilters
    venue_caps: VenueCaps = VenueCaps()
    budgets: UpdateBudgets = UpdateBudgets()
    drawdown_breached: bool = False
    price_eps_ticks: int = PRICE_EPS_TICKS_DEFAULT
    qty_eps_steps: int = QTY_EPS_STEPS_DEFAULT


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteResult:
    """Result of a route() decision.

    Attributes:
        decision: The routing decision enum.
        reason: Machine-readable reason code (SSOT §14.6).
        amend_price: New price for AMEND (None if not amending price).
        amend_qty: New qty for AMEND (None if not amending qty).
        new_price: Price for CANCEL_REPLACE new order (None if not placing).
        new_qty: Qty for CANCEL_REPLACE new order (None if not placing).
        details: JSON-safe dict of decision metadata.
    """

    decision: RouterDecision
    reason: str
    amend_price: Decimal | None = None
    amend_qty: Decimal | None = None
    new_price: Decimal | None = None
    new_qty: Decimal | None = None
    details: dict[str, str | int | bool | None] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _would_cross_spread(intent: OrderIntent, market: MarketSnapshot) -> bool:
    """Check if intent price would cross the spread.

    BUY at or above best_ask = cross. SELL at or below best_bid = cross.
    """
    if intent.side == "BUY" and intent.price >= market.best_ask:
        return True
    return intent.side == "SELL" and intent.price <= market.best_bid


def _check_filters(intent: OrderIntent, filters: ExchangeFilters) -> str | None:
    """Validate intent against exchange filters.

    Returns reason code string if violated, None if all checks pass.
    """
    # tick_size alignment
    if filters.tick_size > 0:
        remainder = intent.price % filters.tick_size
        if remainder != 0:
            return "FILTER_VIOLATION_TICK_SIZE"

    # step_size alignment
    if filters.step_size > 0:
        floored = _floor_to_step(intent.qty, filters.step_size)
        if floored != intent.qty:
            return "FILTER_VIOLATION_STEP_SIZE"

    # min_qty
    if intent.qty < filters.min_qty:
        return "FILTER_VIOLATION_MIN_QTY"

    # min_notional
    if intent.qty * intent.price < filters.min_notional:
        return "FILTER_VIOLATION_MIN_NOTIONAL"

    return None


def _floor_to_step(qty: Decimal, step_size: Decimal) -> Decimal:
    """Floor quantity to nearest step size."""
    if step_size <= 0:
        return qty
    steps = (qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * step_size


def _price_delta_ticks(price_a: Decimal, price_b: Decimal, tick_size: Decimal) -> int:
    """Compute absolute price delta in ticks (integer)."""
    if tick_size <= 0:
        return 0
    delta = abs(price_a - price_b)
    return int((delta / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _qty_delta_steps(qty_a: Decimal, qty_b: Decimal, step_size: Decimal) -> int:
    """Compute absolute qty delta in steps (integer)."""
    if step_size <= 0:
        return 0
    delta = abs(qty_a - qty_b)
    return int((delta / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _has_immutable_change(intent: OrderIntent, existing: ExistingOrder) -> bool:
    """Check if intent requires changes to immutable fields (reduce_only, TIF).

    These fields cannot be amended — require cancel-replace.
    """
    if intent.reduce_only != existing.reduce_only:
        return True
    return intent.time_in_force != existing.time_in_force


# ---------------------------------------------------------------------------
# Core routing function
# ---------------------------------------------------------------------------


def route(inputs: RouterInputs) -> RouteResult:  # noqa: PLR0911, PLR0912
    """Compute routing decision for a single grid level.

    Pure function: no I/O, no side effects, no logging.
    Decision priority (first match wins):
      1. Hard BLOCKs (spread crossing, filter violations)
      2. Budget exhaustion
      3. NOOP (no meaningful change)
      4. Prefer AMEND (when venue supports it and safe)
      5. Fallback CANCEL_REPLACE

    Args:
        inputs: Frozen bundle of all decision inputs.

    Returns:
        RouteResult with decision, reason, and action fields.
    """
    intent = inputs.intent
    existing = inputs.existing
    market = inputs.market
    filters = inputs.filters
    caps = inputs.venue_caps
    budgets = inputs.budgets

    # ------------------------------------------------------------------
    # 1. Hard BLOCKs
    # ------------------------------------------------------------------

    # 1a. Spread crossing
    if _would_cross_spread(intent, market):
        return RouteResult(
            decision=RouterDecision.BLOCK,
            reason="WOULD_CROSS_SPREAD",
            details={
                "intent_side": intent.side,
                "intent_price": str(intent.price),
                "best_bid": str(market.best_bid),
                "best_ask": str(market.best_ask),
            },
        )

    # 1b. Filter violations on desired order
    filter_reason = _check_filters(intent, filters)
    if filter_reason is not None:
        return RouteResult(
            decision=RouterDecision.BLOCK,
            reason=filter_reason,
            details={
                "intent_price": str(intent.price),
                "intent_qty": str(intent.qty),
                "tick_size": str(filters.tick_size),
                "step_size": str(filters.step_size),
                "min_qty": str(filters.min_qty),
                "min_notional": str(filters.min_notional),
            },
        )

    # 1c. Drawdown gate blocks INCREASE_RISK intents (SSOT row 1 / invariant I1)
    # Note: We check drawdown_breached + infer intent from context.
    # For PR1 the caller classifies intent; here we use side heuristic:
    # drawdown_breached=True blocks all non-reduce, non-cancel actions.
    if inputs.drawdown_breached:
        return RouteResult(
            decision=RouterDecision.BLOCK,
            reason="DRAWDOWN_GATE_ACTIVE",
            details={"drawdown_breached": True},
        )

    # ------------------------------------------------------------------
    # 2. Budget exhaustion
    # ------------------------------------------------------------------

    if budgets.updates_remaining <= 0:
        return RouteResult(
            decision=RouterDecision.NOOP,
            reason="RATE_LIMIT_THROTTLE",
            details={"updates_remaining": 0},
        )

    # ------------------------------------------------------------------
    # 3. No existing order => CANCEL_REPLACE (new placement)
    # ------------------------------------------------------------------

    if existing is None:
        # Budget check: CANCEL_REPLACE needs budget
        if budgets.cancel_replace_remaining <= 0:
            return RouteResult(
                decision=RouterDecision.NOOP,
                reason="RATE_LIMIT_THROTTLE",
                details={"cancel_replace_remaining": 0},
            )
        return RouteResult(
            decision=RouterDecision.CANCEL_REPLACE,
            reason="NO_EXISTING_ORDER",
            new_price=intent.price,
            new_qty=intent.qty,
            details={"intent_side": intent.side},
        )

    # ------------------------------------------------------------------
    # From here: existing order IS present
    # ------------------------------------------------------------------

    price_ticks = _price_delta_ticks(intent.price, existing.price, filters.tick_size)
    qty_steps = _qty_delta_steps(intent.qty, existing.qty, filters.step_size)

    # ------------------------------------------------------------------
    # 4. Immutable field change => must CANCEL_REPLACE
    #    (checked before NOOP: same price/qty but different TIF is NOT a NOOP)
    # ------------------------------------------------------------------

    if _has_immutable_change(intent, existing):
        if budgets.cancel_replace_remaining <= 0:
            return RouteResult(
                decision=RouterDecision.NOOP,
                reason="RATE_LIMIT_THROTTLE",
                details={"cancel_replace_remaining": 0, "immutable_change": True},
            )
        return RouteResult(
            decision=RouterDecision.CANCEL_REPLACE,
            reason="CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD",
            new_price=intent.price,
            new_qty=intent.qty,
            details={
                "reduce_only_changed": intent.reduce_only != existing.reduce_only,
                "tif_changed": intent.time_in_force != existing.time_in_force,
            },
        )

    # ------------------------------------------------------------------
    # 5. NOOP — no meaningful change
    # ------------------------------------------------------------------

    if price_ticks < inputs.price_eps_ticks and qty_steps < inputs.qty_eps_steps:
        return RouteResult(
            decision=RouterDecision.NOOP,
            reason="NO_CHANGE_BELOW_EPS",
            details={
                "price_delta_ticks": price_ticks,
                "qty_delta_steps": qty_steps,
                "price_eps_ticks": inputs.price_eps_ticks,
                "qty_eps_steps": inputs.qty_eps_steps,
            },
        )

    # ------------------------------------------------------------------
    # 6. Try AMEND
    # ------------------------------------------------------------------

    need_amend_price = price_ticks >= inputs.price_eps_ticks
    need_amend_qty = qty_steps >= inputs.qty_eps_steps

    can_amend_price = caps.supports_amend_price
    can_amend_qty = caps.supports_amend_qty

    # Can we satisfy all needed amendments?
    amend_possible = True
    if need_amend_price and not can_amend_price:
        amend_possible = False
    if need_amend_qty and not can_amend_qty:
        amend_possible = False

    if amend_possible:
        return RouteResult(
            decision=RouterDecision.AMEND,
            reason="AMEND_SUPPORTED_AND_SAFE",
            amend_price=intent.price if need_amend_price else None,
            amend_qty=intent.qty if need_amend_qty else None,
            details={
                "price_delta_ticks": price_ticks,
                "qty_delta_steps": qty_steps,
                "need_amend_price": need_amend_price,
                "need_amend_qty": need_amend_qty,
            },
        )

    # ------------------------------------------------------------------
    # 7. Fallback: CANCEL_REPLACE (amend not supported for needed fields)
    # ------------------------------------------------------------------

    if budgets.cancel_replace_remaining <= 0:
        return RouteResult(
            decision=RouterDecision.NOOP,
            reason="RATE_LIMIT_THROTTLE",
            details={"cancel_replace_remaining": 0, "amend_unsupported": True},
        )

    return RouteResult(
        decision=RouterDecision.CANCEL_REPLACE,
        reason="AMEND_UNSUPPORTED",
        new_price=intent.price,
        new_qty=intent.qty,
        details={
            "need_amend_price": need_amend_price,
            "need_amend_qty": need_amend_qty,
            "supports_amend_price": can_amend_price,
            "supports_amend_qty": can_amend_qty,
        },
    )
