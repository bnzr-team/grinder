"""Table-driven tests for SmartOrderRouter (Launch-14 PR1).

SSOT: docs/14_SMART_ORDER_ROUTER_SPEC.md

Validates:
- Decision matrix coverage (all rows)
- Constraint/filter validation (tick_size, step_size, min_qty, min_notional)
- Budget exhaustion paths
- Epsilon NOOP (sub-tick / sub-step)
- AMEND vs CANCEL_REPLACE based on venue caps
- Immutable field changes (reduce_only, TIF)
- Spread crossing blocks
- Drawdown gate invariant (I1)
- Determinism invariant (I3)
- Reason code invariant (I5)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.execution.smart_order_router import (
    ExchangeFilters,
    ExistingOrder,
    MarketSnapshot,
    OrderIntent,
    RouterDecision,
    RouterInputs,
    UpdateBudgets,
    VenueCaps,
    route,
)

# ---------------------------------------------------------------------------
# Test fixtures / builders
# ---------------------------------------------------------------------------

_D = Decimal

# Standard filters: tick=0.01, step=0.001, min_qty=0.001, min_notional=5
STD_FILTERS = ExchangeFilters(
    tick_size=_D("0.01"),
    step_size=_D("0.001"),
    min_qty=_D("0.001"),
    min_notional=_D("5"),
)

# Standard market: mid ~100, spread 0.02 (2 ticks)
STD_MARKET = MarketSnapshot(best_bid=_D("99.99"), best_ask=_D("100.01"))

# Standard venue: full amend support
STD_CAPS = VenueCaps(supports_amend_price=True, supports_amend_qty=True)

# Default budgets: plenty of room
STD_BUDGETS = UpdateBudgets(updates_remaining=100, cancel_replace_remaining=50)


def _intent(
    price: str = "99.50",
    qty: str = "1.000",
    side: str = "BUY",
    reduce_only: bool = False,
    tif: str = "GTC",
) -> OrderIntent:
    return OrderIntent(
        price=_D(price),
        qty=_D(qty),
        side=side,
        reduce_only=reduce_only,
        time_in_force=tif,
    )


def _existing(
    order_id: str = "ORD-1",
    price: str = "99.50",
    qty: str = "1.000",
    side: str = "BUY",
    reduce_only: bool = False,
    tif: str = "GTC",
) -> ExistingOrder:
    return ExistingOrder(
        order_id=order_id,
        price=_D(price),
        qty=_D(qty),
        side=side,
        reduce_only=reduce_only,
        time_in_force=tif,
    )


def _inputs(
    intent: OrderIntent | None = None,
    existing: ExistingOrder | None = None,
    market: MarketSnapshot | None = None,
    filters: ExchangeFilters | None = None,
    caps: VenueCaps | None = None,
    budgets: UpdateBudgets | None = None,
    drawdown_breached: bool = False,
    price_eps_ticks: int = 1,
    qty_eps_steps: int = 1,
) -> RouterInputs:
    return RouterInputs(
        intent=intent or _intent(),
        existing=existing,
        market=market or STD_MARKET,
        filters=filters or STD_FILTERS,
        venue_caps=caps or STD_CAPS,
        budgets=budgets or STD_BUDGETS,
        drawdown_breached=drawdown_breached,
        price_eps_ticks=price_eps_ticks,
        qty_eps_steps=qty_eps_steps,
    )


# ===========================================================================
# 1. Hard BLOCKs — spread crossing
# ===========================================================================


class TestSpreadCrossing:
    """BUY >= best_ask or SELL <= best_bid => BLOCK WOULD_CROSS_SPREAD."""

    def test_buy_at_ask_blocked(self) -> None:
        r = route(_inputs(intent=_intent(price="100.01", side="BUY")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "WOULD_CROSS_SPREAD"

    def test_buy_above_ask_blocked(self) -> None:
        r = route(_inputs(intent=_intent(price="100.05", side="BUY")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "WOULD_CROSS_SPREAD"

    def test_sell_at_bid_blocked(self) -> None:
        r = route(_inputs(intent=_intent(price="99.99", side="SELL")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "WOULD_CROSS_SPREAD"

    def test_sell_below_bid_blocked(self) -> None:
        r = route(_inputs(intent=_intent(price="99.95", side="SELL")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "WOULD_CROSS_SPREAD"

    def test_buy_below_ask_not_blocked(self) -> None:
        r = route(_inputs(intent=_intent(price="99.50", side="BUY")))
        assert r.decision != RouterDecision.BLOCK or r.reason != "WOULD_CROSS_SPREAD"

    def test_sell_above_bid_not_blocked(self) -> None:
        r = route(_inputs(intent=_intent(price="100.50", side="SELL")))
        assert r.decision != RouterDecision.BLOCK or r.reason != "WOULD_CROSS_SPREAD"


# ===========================================================================
# 2. Hard BLOCKs — filter violations
# ===========================================================================


class TestFilterViolations:
    """Exchange filter constraint violations => BLOCK."""

    def test_tick_size_violation(self) -> None:
        # Price 99.505 not aligned to tick_size 0.01
        r = route(_inputs(intent=_intent(price="99.505")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "FILTER_VIOLATION_TICK_SIZE"

    def test_step_size_violation(self) -> None:
        # Qty 1.0005 not aligned to step_size 0.001
        r = route(_inputs(intent=_intent(qty="1.0005")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "FILTER_VIOLATION_STEP_SIZE"

    def test_min_qty_violation(self) -> None:
        # Qty 0.005 with min_qty=0.01 (step-aligned but below min_qty)
        f = ExchangeFilters(
            tick_size=_D("0.01"),
            step_size=_D("0.001"),
            min_qty=_D("0.01"),
            min_notional=_D("5"),
        )
        r = route(_inputs(intent=_intent(qty="0.005"), filters=f))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "FILTER_VIOLATION_MIN_QTY"

    def test_min_notional_violation(self) -> None:
        # 0.001 * 99.50 = 0.0995 < min_notional 5
        r = route(_inputs(intent=_intent(qty="0.001", price="99.50")))
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "FILTER_VIOLATION_MIN_NOTIONAL"

    def test_all_filters_pass(self) -> None:
        # 1.000 * 99.50 = 99.50 >= 5
        r = route(_inputs(intent=_intent(price="99.50", qty="1.000")))
        assert r.decision != RouterDecision.BLOCK or "FILTER_VIOLATION" not in r.reason

    def test_tick_size_zero_skips_check(self) -> None:
        # tick_size=0 should skip tick check
        f = ExchangeFilters(
            tick_size=_D("0"),
            step_size=_D("0.001"),
            min_qty=_D("0.001"),
            min_notional=_D("5"),
        )
        r = route(_inputs(intent=_intent(price="99.505"), filters=f))
        assert r.reason != "FILTER_VIOLATION_TICK_SIZE"

    def test_step_size_zero_skips_check(self) -> None:
        f = ExchangeFilters(
            tick_size=_D("0.01"),
            step_size=_D("0"),
            min_qty=_D("0.001"),
            min_notional=_D("5"),
        )
        r = route(_inputs(intent=_intent(qty="1.0005"), filters=f))
        assert r.reason != "FILTER_VIOLATION_STEP_SIZE"


# ===========================================================================
# 3. Hard BLOCK — drawdown gate (invariant I1)
# ===========================================================================


class TestDrawdownGate:
    """drawdown_breached=True => BLOCK DRAWDOWN_GATE_ACTIVE."""

    def test_drawdown_blocks_buy(self) -> None:
        r = route(
            _inputs(
                intent=_intent(side="BUY"),
                drawdown_breached=True,
            )
        )
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "DRAWDOWN_GATE_ACTIVE"

    def test_drawdown_blocks_sell(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="100.50", side="SELL"),
                drawdown_breached=True,
            )
        )
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "DRAWDOWN_GATE_ACTIVE"

    def test_drawdown_blocks_with_existing(self) -> None:
        r = route(
            _inputs(
                intent=_intent(side="BUY"),
                existing=_existing(side="BUY"),
                drawdown_breached=True,
            )
        )
        assert r.decision == RouterDecision.BLOCK
        assert r.reason == "DRAWDOWN_GATE_ACTIVE"

    def test_no_drawdown_not_blocked(self) -> None:
        r = route(
            _inputs(
                intent=_intent(side="BUY"),
                drawdown_breached=False,
            )
        )
        assert r.reason != "DRAWDOWN_GATE_ACTIVE"


# ===========================================================================
# 4. Budget exhaustion
# ===========================================================================


class TestBudgetExhaustion:
    """Rate-limit budget => NOOP RATE_LIMIT_THROTTLE."""

    def test_updates_budget_zero_noop(self) -> None:
        r = route(
            _inputs(
                budgets=UpdateBudgets(updates_remaining=0, cancel_replace_remaining=50),
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "RATE_LIMIT_THROTTLE"

    def test_cancel_replace_budget_zero_no_existing_noop(self) -> None:
        # No existing order, need CANCEL_REPLACE but budget=0
        r = route(
            _inputs(
                budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0),
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "RATE_LIMIT_THROTTLE"

    def test_cancel_replace_budget_zero_but_amend_possible(self) -> None:
        # Existing order with price change, amend supported, c/r budget=0
        # Should still AMEND (amend doesn't need cancel_replace budget)
        r = route(
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.reason == "AMEND_SUPPORTED_AND_SAFE"

    def test_cancel_replace_budget_zero_amend_unsupported_noop(self) -> None:
        # Need price amend but not supported, c/r budget=0
        r = route(
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=True),
                budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0),
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "RATE_LIMIT_THROTTLE"

    def test_cancel_replace_budget_zero_immutable_change_noop(self) -> None:
        # Immutable change (TIF) but c/r budget=0
        r = route(
            _inputs(
                intent=_intent(price="99.50", tif="IOC"),
                existing=_existing(price="99.50", tif="GTC"),
                budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0),
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "RATE_LIMIT_THROTTLE"


# ===========================================================================
# 5. NOOP — no meaningful change (epsilon)
# ===========================================================================


class TestNoopEpsilon:
    """Sub-epsilon deltas => NOOP NO_CHANGE_BELOW_EPS."""

    def test_exact_match_noop(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.50", qty="1.000"),
                existing=_existing(price="99.50", qty="1.000"),
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "NO_CHANGE_BELOW_EPS"

    def test_sub_tick_price_noop(self) -> None:
        # price_eps_ticks=2, delta=1 tick => still NOOP
        r = route(
            _inputs(
                intent=_intent(price="99.51"),
                existing=_existing(price="99.50"),
                price_eps_ticks=2,
                qty_eps_steps=1,
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "NO_CHANGE_BELOW_EPS"

    def test_at_eps_threshold_triggers_action(self) -> None:
        # price_eps_ticks=1, delta=1 tick => NOT noop (>= eps)
        r = route(
            _inputs(
                intent=_intent(price="99.51"),
                existing=_existing(price="99.50"),
                price_eps_ticks=1,
            )
        )
        assert r.decision != RouterDecision.NOOP or r.reason != "NO_CHANGE_BELOW_EPS"

    def test_sub_step_qty_noop(self) -> None:
        # qty_eps_steps=2, delta=1 step => NOOP
        r = route(
            _inputs(
                intent=_intent(qty="1.001"),
                existing=_existing(qty="1.000"),
                qty_eps_steps=2,
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.reason == "NO_CHANGE_BELOW_EPS"

    def test_price_change_but_qty_below_eps(self) -> None:
        # Price changed (>= eps), qty sub-eps => NOT noop
        r = route(
            _inputs(
                intent=_intent(price="99.60", qty="1.000"),
                existing=_existing(price="99.50", qty="1.000"),
            )
        )
        assert r.decision != RouterDecision.NOOP

    def test_qty_change_but_price_below_eps(self) -> None:
        # Qty changed (>= eps), price identical => NOT noop
        r = route(
            _inputs(
                intent=_intent(price="99.50", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
            )
        )
        assert r.decision != RouterDecision.NOOP


# ===========================================================================
# 6. No existing order => CANCEL_REPLACE
# ===========================================================================


class TestNoExistingOrder:
    """No existing order => CANCEL_REPLACE NO_EXISTING_ORDER."""

    def test_no_existing_cancel_replace(self) -> None:
        r = route(_inputs(existing=None))
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "NO_EXISTING_ORDER"
        assert r.new_price == _D("99.50")
        assert r.new_qty == _D("1.000")

    def test_no_existing_sell(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="100.50", side="SELL"),
                existing=None,
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "NO_EXISTING_ORDER"


# ===========================================================================
# 7. Immutable field changes => CANCEL_REPLACE
# ===========================================================================


class TestImmutableFieldChanges:
    """reduce_only or TIF change => CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD."""

    def test_reduce_only_change(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.50", reduce_only=True),
                existing=_existing(price="99.50", reduce_only=False),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD"

    def test_tif_change(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.50", tif="GTX"),
                existing=_existing(price="99.50", tif="GTC"),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD"

    def test_both_immutable_fields_change(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.50", reduce_only=True, tif="IOC"),
                existing=_existing(price="99.50", reduce_only=False, tif="GTC"),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD"

    def test_immutable_fields_same_no_trigger(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
            )
        )
        assert r.reason != "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD"


# ===========================================================================
# 8. AMEND — venue supports it
# ===========================================================================


class TestAmendPath:
    """AMEND when venue supports and safe."""

    def test_price_amend(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.reason == "AMEND_SUPPORTED_AND_SAFE"
        assert r.amend_price == _D("99.60")
        assert r.amend_qty is None

    def test_qty_amend(self) -> None:
        r = route(
            _inputs(
                intent=_intent(qty="1.010"),
                existing=_existing(qty="1.000"),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.reason == "AMEND_SUPPORTED_AND_SAFE"
        assert r.amend_price is None
        assert r.amend_qty == _D("1.010")

    def test_both_price_and_qty_amend(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.60", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.amend_price == _D("99.60")
        assert r.amend_qty == _D("1.010")

    def test_large_price_delta_still_amend_if_supported(self) -> None:
        # SOR per marching orders prefers AMEND when venue supports it
        r = route(
            _inputs(
                intent=_intent(price="98.00"),
                existing=_existing(price="99.50"),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.reason == "AMEND_SUPPORTED_AND_SAFE"


# ===========================================================================
# 9. CANCEL_REPLACE — amend not supported
# ===========================================================================


class TestCancelReplaceFallback:
    """CANCEL_REPLACE when amend not supported for needed fields."""

    def test_price_amend_unsupported(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=True),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "AMEND_UNSUPPORTED"
        assert r.new_price == _D("99.60")
        assert r.new_qty == _D("1.000")

    def test_qty_amend_unsupported(self) -> None:
        r = route(
            _inputs(
                intent=_intent(qty="1.010"),
                existing=_existing(qty="1.000"),
                caps=VenueCaps(supports_amend_price=True, supports_amend_qty=False),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "AMEND_UNSUPPORTED"

    def test_both_amend_unsupported(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.60", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=False),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "AMEND_UNSUPPORTED"

    def test_only_qty_change_but_qty_amend_unsupported(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.50", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
                caps=VenueCaps(supports_amend_price=True, supports_amend_qty=False),
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.reason == "AMEND_UNSUPPORTED"

    def test_only_price_change_qty_amend_unsupported_still_amend(self) -> None:
        # Only price changed, price amend IS supported, qty amend unsupported irrelevant
        r = route(
            _inputs(
                intent=_intent(price="99.60", qty="1.000"),
                existing=_existing(price="99.50", qty="1.000"),
                caps=VenueCaps(supports_amend_price=True, supports_amend_qty=False),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.reason == "AMEND_SUPPORTED_AND_SAFE"


# ===========================================================================
# 10. Decision priority — hard BLOCKs outrank everything
# ===========================================================================


class TestDecisionPriority:
    """Verify priority ordering of decision rules."""

    def test_spread_crossing_outranks_drawdown(self) -> None:
        # Both spread crossing AND drawdown, but spread check comes first
        r = route(
            _inputs(
                intent=_intent(price="100.01", side="BUY"),
                drawdown_breached=True,
            )
        )
        assert r.reason == "WOULD_CROSS_SPREAD"

    def test_filter_violation_outranks_drawdown(self) -> None:
        # Both filter violation AND drawdown
        r = route(
            _inputs(
                intent=_intent(price="99.505"),
                drawdown_breached=True,
            )
        )
        assert r.reason == "FILTER_VIOLATION_TICK_SIZE"

    def test_drawdown_outranks_budget(self) -> None:
        r = route(
            _inputs(
                drawdown_breached=True,
                budgets=UpdateBudgets(updates_remaining=0, cancel_replace_remaining=0),
            )
        )
        assert r.reason == "DRAWDOWN_GATE_ACTIVE"

    def test_budget_outranks_no_existing(self) -> None:
        r = route(
            _inputs(
                existing=None,
                budgets=UpdateBudgets(updates_remaining=0, cancel_replace_remaining=50),
            )
        )
        assert r.reason == "RATE_LIMIT_THROTTLE"

    def test_noop_outranks_amend(self) -> None:
        # Exact match with existing => NOOP, not AMEND
        r = route(
            _inputs(
                intent=_intent(price="99.50", qty="1.000"),
                existing=_existing(price="99.50", qty="1.000"),
            )
        )
        assert r.decision == RouterDecision.NOOP

    def test_immutable_outranks_amend(self) -> None:
        # TIF change with price change => CANCEL_REPLACE immutable, not AMEND
        r = route(
            _inputs(
                intent=_intent(price="99.60", tif="IOC"),
                existing=_existing(price="99.50", tif="GTC"),
            )
        )
        assert r.reason == "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD"


# ===========================================================================
# 11. Invariants (SSOT §14.8)
# ===========================================================================


class TestInvariants:
    """SSOT invariants I1-I6."""

    def test_i1_drawdown_never_increases_risk(self) -> None:
        """I1: SOR never increases risk when drawdown_breached=True."""
        # Try many different intents with drawdown active
        for side, price in [("BUY", "99.50"), ("SELL", "100.50")]:
            for existing in [None, _existing(price=price, side=side)]:
                r = route(
                    _inputs(
                        intent=_intent(price=price, side=side),
                        existing=existing,
                        drawdown_breached=True,
                    )
                )
                assert r.decision == RouterDecision.BLOCK
                assert r.reason == "DRAWDOWN_GATE_ACTIVE"

    def test_i2_no_constraint_violations_in_non_block_results(self) -> None:
        """I2: SOR never produces actions that violate exchange constraints.

        If filters pass, any non-BLOCK result has valid price/qty.
        """
        r = route(
            _inputs(
                intent=_intent(price="99.50", qty="1.000"),
                existing=None,
            )
        )
        assert r.decision == RouterDecision.CANCEL_REPLACE
        # new_price/new_qty should match intent (which passed filters)
        assert r.new_price == _D("99.50")
        assert r.new_qty == _D("1.000")

    def test_i3_determinism(self) -> None:
        """I3: Same inputs => identical output (10 runs)."""
        inp = _inputs(
            intent=_intent(price="99.60"),
            existing=_existing(price="99.50"),
        )
        first = route(inp)
        for _ in range(9):
            result = route(inp)
            assert result.decision == first.decision
            assert result.reason == first.reason
            assert result.amend_price == first.amend_price
            assert result.amend_qty == first.amend_qty
            assert result.new_price == first.new_price
            assert result.new_qty == first.new_qty

    def test_i5_every_result_has_reason(self) -> None:
        """I5: Every decision emits a reason code."""
        cases = [
            _inputs(intent=_intent(price="100.01", side="BUY")),  # spread crossing
            _inputs(intent=_intent(price="99.505")),  # filter violation
            _inputs(drawdown_breached=True),  # drawdown
            _inputs(budgets=UpdateBudgets(updates_remaining=0, cancel_replace_remaining=0)),
            _inputs(existing=None),  # no existing
            _inputs(  # exact match
                intent=_intent(price="99.50"),
                existing=_existing(price="99.50"),
            ),
            _inputs(  # price change
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
            ),
        ]
        for inp in cases:
            r = route(inp)
            assert r.reason, f"Empty reason for decision {r.decision}"
            assert isinstance(r.reason, str)
            assert len(r.reason) > 0

    def test_i6_amend_only_when_venue_supports(self) -> None:
        """I6: AMEND is only chosen when exchange supports it."""
        # No amend support => never AMEND
        r = route(
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=False),
            )
        )
        assert r.decision != RouterDecision.AMEND


# ===========================================================================
# 12. RouteResult output fields
# ===========================================================================


class TestRouteResultFields:
    """Verify output fields are populated correctly per decision type."""

    def test_block_has_no_action_fields(self) -> None:
        r = route(_inputs(intent=_intent(price="100.01", side="BUY")))
        assert r.decision == RouterDecision.BLOCK
        assert r.amend_price is None
        assert r.amend_qty is None
        assert r.new_price is None
        assert r.new_qty is None

    def test_noop_has_no_action_fields(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.50"),
                existing=_existing(price="99.50"),
            )
        )
        assert r.decision == RouterDecision.NOOP
        assert r.amend_price is None
        assert r.amend_qty is None
        assert r.new_price is None
        assert r.new_qty is None

    def test_amend_has_amend_fields(self) -> None:
        r = route(
            _inputs(
                intent=_intent(price="99.60", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
            )
        )
        assert r.decision == RouterDecision.AMEND
        assert r.amend_price is not None or r.amend_qty is not None
        assert r.new_price is None
        assert r.new_qty is None

    def test_cancel_replace_has_new_fields(self) -> None:
        r = route(_inputs(existing=None))
        assert r.decision == RouterDecision.CANCEL_REPLACE
        assert r.new_price is not None
        assert r.new_qty is not None
        assert r.amend_price is None
        assert r.amend_qty is None

    def test_details_always_dict(self) -> None:
        r = route(_inputs())
        assert isinstance(r.details, dict)


# ===========================================================================
# 13. Table-driven parametrized test (comprehensive matrix)
# ===========================================================================


@pytest.mark.parametrize(
    "test_id, inp, expected_decision, expected_reason",
    [
        # --- Hard BLOCKs ---
        (
            "B01_buy_at_ask",
            _inputs(intent=_intent(price="100.01", side="BUY")),
            RouterDecision.BLOCK,
            "WOULD_CROSS_SPREAD",
        ),
        (
            "B02_sell_at_bid",
            _inputs(intent=_intent(price="99.99", side="SELL")),
            RouterDecision.BLOCK,
            "WOULD_CROSS_SPREAD",
        ),
        (
            "B03_tick_violation",
            _inputs(intent=_intent(price="99.555")),
            RouterDecision.BLOCK,
            "FILTER_VIOLATION_TICK_SIZE",
        ),
        (
            "B04_step_violation",
            _inputs(intent=_intent(qty="1.0005")),
            RouterDecision.BLOCK,
            "FILTER_VIOLATION_STEP_SIZE",
        ),
        (
            "B05_min_qty",
            _inputs(
                intent=_intent(qty="0.005"),
                filters=ExchangeFilters(
                    tick_size=_D("0.01"),
                    step_size=_D("0.001"),
                    min_qty=_D("0.01"),
                    min_notional=_D("5"),
                ),
            ),
            RouterDecision.BLOCK,
            "FILTER_VIOLATION_MIN_QTY",
        ),
        (
            "B06_min_notional",
            _inputs(intent=_intent(qty="0.001", price="99.50")),
            RouterDecision.BLOCK,
            "FILTER_VIOLATION_MIN_NOTIONAL",
        ),
        (
            "B07_drawdown_buy",
            _inputs(drawdown_breached=True),
            RouterDecision.BLOCK,
            "DRAWDOWN_GATE_ACTIVE",
        ),
        (
            "B08_drawdown_sell",
            _inputs(intent=_intent(price="100.50", side="SELL"), drawdown_breached=True),
            RouterDecision.BLOCK,
            "DRAWDOWN_GATE_ACTIVE",
        ),
        (
            "B09_drawdown_with_existing",
            _inputs(existing=_existing(), drawdown_breached=True),
            RouterDecision.BLOCK,
            "DRAWDOWN_GATE_ACTIVE",
        ),
        # --- Budget exhaustion ---
        (
            "BU01_updates_zero",
            _inputs(budgets=UpdateBudgets(updates_remaining=0, cancel_replace_remaining=50)),
            RouterDecision.NOOP,
            "RATE_LIMIT_THROTTLE",
        ),
        (
            "BU02_cr_zero_no_existing",
            _inputs(budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0)),
            RouterDecision.NOOP,
            "RATE_LIMIT_THROTTLE",
        ),
        # --- No existing order ---
        (
            "NE01_no_existing_buy",
            _inputs(existing=None),
            RouterDecision.CANCEL_REPLACE,
            "NO_EXISTING_ORDER",
        ),
        (
            "NE02_no_existing_sell",
            _inputs(intent=_intent(price="100.50", side="SELL"), existing=None),
            RouterDecision.CANCEL_REPLACE,
            "NO_EXISTING_ORDER",
        ),
        # --- NOOP epsilon ---
        (
            "EP01_exact_match",
            _inputs(intent=_intent(price="99.50"), existing=_existing(price="99.50")),
            RouterDecision.NOOP,
            "NO_CHANGE_BELOW_EPS",
        ),
        (
            "EP02_sub_tick",
            _inputs(
                intent=_intent(price="99.51"), existing=_existing(price="99.50"), price_eps_ticks=2
            ),
            RouterDecision.NOOP,
            "NO_CHANGE_BELOW_EPS",
        ),
        (
            "EP03_sub_step",
            _inputs(intent=_intent(qty="1.001"), existing=_existing(qty="1.000"), qty_eps_steps=2),
            RouterDecision.NOOP,
            "NO_CHANGE_BELOW_EPS",
        ),
        # --- Immutable changes ---
        (
            "IM01_reduce_only_change",
            _inputs(
                intent=_intent(price="99.50", reduce_only=True),
                existing=_existing(price="99.50", reduce_only=False),
            ),
            RouterDecision.CANCEL_REPLACE,
            "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD",
        ),
        (
            "IM02_tif_change",
            _inputs(
                intent=_intent(price="99.50", tif="IOC"),
                existing=_existing(price="99.50", tif="GTC"),
            ),
            RouterDecision.CANCEL_REPLACE,
            "CANCEL_REPLACE_REQUIRED_IMMUTABLE_FIELD",
        ),
        # --- AMEND ---
        (
            "AM01_price_amend",
            _inputs(intent=_intent(price="99.60"), existing=_existing(price="99.50")),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        (
            "AM02_qty_amend",
            _inputs(intent=_intent(qty="1.010"), existing=_existing(qty="1.000")),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        (
            "AM03_both_amend",
            _inputs(
                intent=_intent(price="99.60", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
            ),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        (
            "AM04_large_price_delta_amend",
            _inputs(intent=_intent(price="98.00"), existing=_existing(price="99.50")),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        # --- CANCEL_REPLACE (amend unsupported) ---
        (
            "CR01_price_amend_unsupported",
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=True),
            ),
            RouterDecision.CANCEL_REPLACE,
            "AMEND_UNSUPPORTED",
        ),
        (
            "CR02_qty_amend_unsupported",
            _inputs(
                intent=_intent(qty="1.010"),
                existing=_existing(qty="1.000"),
                caps=VenueCaps(supports_amend_price=True, supports_amend_qty=False),
            ),
            RouterDecision.CANCEL_REPLACE,
            "AMEND_UNSUPPORTED",
        ),
        (
            "CR03_both_unsupported",
            _inputs(
                intent=_intent(price="99.60", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=False),
            ),
            RouterDecision.CANCEL_REPLACE,
            "AMEND_UNSUPPORTED",
        ),
        # --- Mixed: only one field changed, irrelevant cap missing ---
        (
            "MX01_price_only_qty_cap_missing",
            _inputs(
                intent=_intent(price="99.60", qty="1.000"),
                existing=_existing(price="99.50", qty="1.000"),
                caps=VenueCaps(supports_amend_price=True, supports_amend_qty=False),
            ),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        (
            "MX02_qty_only_price_cap_missing",
            _inputs(
                intent=_intent(price="99.50", qty="1.010"),
                existing=_existing(price="99.50", qty="1.000"),
                caps=VenueCaps(supports_amend_price=False, supports_amend_qty=True),
            ),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        # --- Budget + amend interaction ---
        (
            "BA01_cr_zero_amend_ok",
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0),
            ),
            RouterDecision.AMEND,
            "AMEND_SUPPORTED_AND_SAFE",
        ),
        (
            "BA02_cr_zero_amend_unsupported",
            _inputs(
                intent=_intent(price="99.60"),
                existing=_existing(price="99.50"),
                caps=VenueCaps(supports_amend_price=False),
                budgets=UpdateBudgets(updates_remaining=100, cancel_replace_remaining=0),
            ),
            RouterDecision.NOOP,
            "RATE_LIMIT_THROTTLE",
        ),
    ],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_decision_matrix(
    test_id: str,
    inp: RouterInputs,
    expected_decision: RouterDecision,
    expected_reason: str,
) -> None:
    """Table-driven decision matrix test."""
    result = route(inp)
    assert result.decision == expected_decision, (
        f"[{test_id}] expected {expected_decision.value}, got {result.decision.value} "
        f"(reason={result.reason})"
    )
    assert result.reason == expected_reason, (
        f"[{test_id}] expected reason={expected_reason}, got {result.reason}"
    )


# ===========================================================================
# 14. Determinism stress test (I3)
# ===========================================================================


class TestDeterminismStress:
    """Run identical inputs many times, verify identical output."""

    @pytest.mark.parametrize("_run", range(10))
    def test_determinism_10_runs(self, _run: int) -> None:
        inp = _inputs(
            intent=_intent(price="99.60", qty="1.010"),
            existing=_existing(price="99.50", qty="1.000"),
        )
        r = route(inp)
        assert r.decision == RouterDecision.AMEND
        assert r.reason == "AMEND_SUPPORTED_AND_SAFE"
        assert r.amend_price == _D("99.60")
        assert r.amend_qty == _D("1.010")
