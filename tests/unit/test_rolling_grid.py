"""PR-ROLLING-GRID-V1A: Rolling grid planner tests (doc-26 T1-T20 subset).

Tests the planner-only rolling grid building blocks:
- _RollingLadderState and state management
- Additive level formulas (_build_rolling_grid)
- Price-based order matching (_match_orders_by_price)
- plan() with rolling_mode=True vs False

No engine integration tests here — those are deferred to V1B.
"""

from __future__ import annotations

from decimal import Decimal

from grinder.account.contracts import OpenOrderSnap
from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.live.grid_planner import LiveGridConfig, LiveGridPlannerV1


def _make_config(
    *,
    levels: int = 3,
    spacing_bps: float = 10.0,
    tick_size: str = "0.10",
    size: str = "0.01",
    epsilon_bps: float = 0.5,
) -> LiveGridConfig:
    return LiveGridConfig(
        base_spacing_bps=spacing_bps,
        levels=levels,
        size_per_level=Decimal(size),
        tick_size=Decimal(tick_size),
        price_epsilon_bps=epsilon_bps,
    )


def _make_order(
    order_id: str,
    symbol: str,
    side: str,
    price: str,
    qty: str = "0.01",
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal(qty),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=1000000,
    )


# ===== Planner unit constants =====
SYMBOL = "BTCUSDT"
ANCHOR = Decimal("66000")
SPACING_BPS = 10.0
# step_price = round_to_tick(66000 * 10 / 10000) = round_to_tick(66.0) = 66.0
STEP = Decimal("66.0")
TICK = Decimal("0.10")


def _make_planner(levels: int = 3) -> LiveGridPlannerV1:
    return LiveGridPlannerV1(
        _make_config(levels=levels, spacing_bps=SPACING_BPS, tick_size=str(TICK))
    )


def _build_full_grid_orders(
    planner: LiveGridPlannerV1, anchor: Decimal = ANCHOR, net_offset: int = 0
) -> tuple[OpenOrderSnap, ...]:
    """Build exchange orders that match the desired grid at given offset."""
    planner.init_rolling_state(SYMBOL, anchor, SPACING_BPS)
    st = planner.get_rolling_state(SYMBOL)
    assert st is not None
    st.net_offset = net_offset
    ec = anchor + net_offset * STEP
    orders: list[OpenOrderSnap] = []
    cfg = planner._config
    for i in range(1, cfg.levels + 1):
        buy_price = ec - i * STEP
        sell_price = ec + i * STEP
        orders.append(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}",
                SYMBOL,
                "BUY",
                str(buy_price),
            )
        )
        orders.append(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i + 100}",
                SYMBOL,
                "SELL",
                str(sell_price),
            )
        )
    return tuple(orders)


class TestRollingGridPlanner:
    """Planner-only rolling grid tests (T1-T10, T16-T17, T19-T20)."""

    def test_t1_buy_fill_cardinality(self) -> None:
        """T1 (INV-1): After 1 BUY fill, desired has N_buy + N_sell levels."""
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        # Simulate BUY fill: remove lowest BUY order
        planner.apply_fill_offset(SYMBOL, "BUY")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )
        # 3 BUY + 3 SELL = 6 desired levels
        assert result.desired_count == 6

    def test_t2_sell_fill_cardinality(self) -> None:
        """T2 (INV-1): After 1 SELL fill, N_buy + N_sell preserved."""
        planner = _make_planner(levels=3)
        orders = _build_full_grid_orders(planner)

        planner.apply_fill_offset(SYMBOL, "SELL")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )
        assert result.desired_count == 6

    def test_t3_spacing_uniform(self) -> None:
        """T3 (INV-2): Adjacent desired levels differ by exactly step_price."""
        planner = _make_planner(levels=5)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # Apply some fills to get non-zero offset
        planner.apply_fill_offset(SYMBOL, "BUY")
        planner.apply_fill_offset(SYMBOL, "BUY")

        levels = planner._build_rolling_grid(SYMBOL)

        buy_prices = sorted([lv.price for lv in levels if lv.side == OrderSide.BUY], reverse=True)
        sell_prices = sorted([lv.price for lv in levels if lv.side == OrderSide.SELL])

        for i in range(len(buy_prices) - 1):
            diff = buy_prices[i] - buy_prices[i + 1]
            assert diff == STEP, f"BUY spacing at {i}: {diff} != {STEP}"

        for i in range(len(sell_prices) - 1):
            diff = sell_prices[i + 1] - sell_prices[i]
            assert diff == STEP, f"SELL spacing at {i}: {diff} != {STEP}"

    def test_t4_buy_fill_center_shift(self) -> None:
        """T4 (INV-3): After 1 BUY fill, ec decreased by step_price."""
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == 0

        planner.apply_fill_offset(SYMBOL, "BUY")

        assert st.net_offset == -1
        ec = st.anchor_price + st.net_offset * st.step_price
        assert ec == ANCHOR - STEP

    def test_t5_k_buy_fills_center_shift(self) -> None:
        """T5 (INV-3): After k=3 BUY fills, ec decreased by 3*step_price."""
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        for _ in range(3):
            planner.apply_fill_offset(SYMBOL, "BUY")

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == -3
        ec = st.anchor_price + st.net_offset * st.step_price
        assert ec == ANCHOR - 3 * STEP

    def test_t6_sell_fill_center_shift(self) -> None:
        """T6 (INV-3): After 1 SELL fill, ec increased by step_price."""
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        planner.apply_fill_offset(SYMBOL, "SELL")

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == 1
        ec = st.anchor_price + st.net_offset * st.step_price
        assert ec == ANCHOR + STEP

    def test_t7_frontier_placement(self) -> None:
        """T7 (INV-4): New orders at frontier, not center.

        After BUY fill: new PLACE should be at farthest BUY (frontier)
        and new SELL closer to center (inner frontier).
        """
        planner = _make_planner(levels=3)
        # Build full grid at offset=0
        orders = _build_full_grid_orders(planner, net_offset=0)
        # Simulate BUY fill: offset becomes -1
        planner.apply_fill_offset(SYMBOL, "BUY")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )

        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        # After shift down, we expect PLACEs at frontier positions
        # (new lowest BUY and new innermost SELL), not at center
        assert len(places) >= 1

        # Verify no PLACE is at center (ec)
        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        ec = st.anchor_price + st.net_offset * st.step_price
        for p in places:
            assert p.price != ec, f"PLACE at center {ec} — should be at frontier"

    def test_t8_fill_action_count(self) -> None:
        """T8 (INV-5): Single fill in steady-state → 1 CANCEL + 2 PLACE = 3 actions.

        Steady-state = full grid established, no budget/guard interference.
        """
        planner = _make_planner(levels=3)
        # Build full grid at offset=0
        full_orders = _build_full_grid_orders(planner, net_offset=0)

        # Simulate 1 BUY fill: remove the closest BUY (L1)
        planner.apply_fill_offset(SYMBOL, "BUY")

        # Remove the filled BUY L1 from open orders
        remaining = tuple(
            o for o in full_orders if not (o.side == "BUY" and o.price == ANCHOR - STEP)
        )

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=remaining,
            rolling_mode=True,
        )

        cancels = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        places = [a for a in result.actions if a.action_type == ActionType.PLACE]

        # 1 CANCEL (farthest SELL now extra) + 2 PLACE (new frontier BUY + inner SELL)
        assert len(cancels) == 1, f"Expected 1 CANCEL, got {len(cancels)}"
        assert len(places) == 2, f"Expected 2 PLACE, got {len(places)}"

    def test_t9_tp_fill_no_offset_change(self) -> None:
        """T9 (INV-6): TP fill does NOT change net_offset.

        apply_fill_offset is only called for grid orders, not TPs.
        This test verifies the API contract — engine is responsible for
        NOT calling apply_fill_offset for TP orders.
        """
        planner = _make_planner()
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        # No calls to apply_fill_offset for TP fills
        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.net_offset == 0

        # Only grid fills change offset
        planner.apply_fill_offset(SYMBOL, "BUY")
        assert st.net_offset == -1

        # TP fill = no call → offset unchanged
        assert st.net_offset == -1

    def test_t10_buy_sell_symmetry(self) -> None:
        """T10 (INV-7): BUY fill path mirrors SELL fill path."""
        planner_buy = _make_planner(levels=3)
        planner_sell = _make_planner(levels=3)

        # BUY fill
        orders_buy = _build_full_grid_orders(planner_buy, net_offset=0)
        planner_buy.apply_fill_offset(SYMBOL, "BUY")
        remaining_buy = tuple(
            o for o in orders_buy if not (o.side == "BUY" and o.price == ANCHOR - STEP)
        )
        result_buy = planner_buy.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=remaining_buy,
            rolling_mode=True,
        )

        # SELL fill
        orders_sell = _build_full_grid_orders(planner_sell, net_offset=0)
        planner_sell.apply_fill_offset(SYMBOL, "SELL")
        remaining_sell = tuple(
            o for o in orders_sell if not (o.side == "SELL" and o.price == ANCHOR + STEP)
        )
        result_sell = planner_sell.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=remaining_sell,
            rolling_mode=True,
        )

        # Same action shape: 1 CANCEL + 2 PLACE
        buy_cancels = sum(1 for a in result_buy.actions if a.action_type == ActionType.CANCEL)
        buy_places = sum(1 for a in result_buy.actions if a.action_type == ActionType.PLACE)
        sell_cancels = sum(1 for a in result_sell.actions if a.action_type == ActionType.CANCEL)
        sell_places = sum(1 for a in result_sell.actions if a.action_type == ActionType.PLACE)

        assert buy_cancels == sell_cancels, "BUY/SELL CANCEL count mismatch"
        assert buy_places == sell_places, "BUY/SELL PLACE count mismatch"

        # Opposite offsets
        st_buy = planner_buy.get_rolling_state(SYMBOL)
        st_sell = planner_sell.get_rolling_state(SYMBOL)
        assert st_buy is not None and st_sell is not None
        assert st_buy.net_offset == -st_sell.net_offset

    def test_t16_price_matching_ignores_level_id(self) -> None:
        """T16 (SS 9.2): Correct price, wrong level_id → MATCH (not extra).

        After rolling shift, exchange orders retain original level_id from
        previous center. Price matching should still match by price.
        """
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        ec = ANCHOR
        # Create exchange orders with wrong level_ids but correct prices
        orders = (
            _make_order("grinder_d_BTCUSDT_99_1000000_1", SYMBOL, "BUY", str(ec - STEP)),
            _make_order("grinder_d_BTCUSDT_88_1000000_2", SYMBOL, "BUY", str(ec - 2 * STEP)),
            _make_order("grinder_d_BTCUSDT_77_1000000_3", SYMBOL, "BUY", str(ec - 3 * STEP)),
            _make_order("grinder_d_BTCUSDT_66_1000000_4", SYMBOL, "SELL", str(ec + STEP)),
            _make_order("grinder_d_BTCUSDT_55_1000000_5", SYMBOL, "SELL", str(ec + 2 * STEP)),
            _make_order("grinder_d_BTCUSDT_44_1000000_6", SYMBOL, "SELL", str(ec + 3 * STEP)),
        )

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=orders,
            rolling_mode=True,
        )

        # All match by price — 0 actions
        assert result.diff_extra == 0, f"Expected 0 extra, got {result.diff_extra}"
        assert result.diff_missing == 0, f"Expected 0 missing, got {result.diff_missing}"
        assert len(result.actions) == 0

    def test_t17_mid_drift_zero_actions(self) -> None:
        """T17 (SS 6.5): Mid-price drift → 0 planner actions.

        Rolling grid doesn't track mid_price. Different mid should not
        cause any actions if exchange orders match desired prices.
        """
        planner = _make_planner(levels=3)
        full_orders = _build_full_grid_orders(planner, net_offset=0)

        # Price drifted significantly but no fills
        drifted_mid = ANCHOR + Decimal("500")

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=drifted_mid,
            ts_ms=1000000,
            open_orders=full_orders,
            rolling_mode=True,
        )

        # 0 actions — grid is anchored to rolling state, not mid_price
        assert len(result.actions) == 0

    def test_t19_flag_off_uses_mid_anchored(self) -> None:
        """T19 (SS 14): rolling_mode=False uses mid-anchored grid (existing behavior)."""
        planner = _make_planner(levels=3)

        # With rolling_mode=False, no rolling state should be created
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=(),
            rolling_mode=False,
        )

        # Should create grid centered on mid_price (all missing = GRID_FILL)
        assert result.desired_count == 6
        assert result.diff_missing == 6
        places = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(places) == 6

        # No rolling state created
        assert planner.get_rolling_state(SYMBOL) is None

    def test_t20_flag_off_grid_shift_on_mid_drift(self) -> None:
        """T20 (SS 14): rolling_mode=False → GRID_SHIFT on mid drift."""
        planner = _make_planner(levels=3)

        # First plan at original mid
        full_orders = tuple(
            _make_order(
                f"grinder_d_{SYMBOL}_{i}_{1000000}_{i}",
                SYMBOL,
                side,
                str(
                    ANCHOR
                    + (Decimal(str(i)) if side == "SELL" else -Decimal(str(i)))
                    * ANCHOR
                    * Decimal("10")
                    / Decimal("10000")
                ),
            )
            for i in range(1, 4)
            for side in ("BUY", "SELL")
        )

        # With rolling_mode=False and large mid shift, planner should produce actions
        drifted_mid = ANCHOR + Decimal("1000")
        result = planner.plan(
            symbol=SYMBOL,
            mid_price=drifted_mid,
            ts_ms=2000000,
            open_orders=full_orders,
            rolling_mode=False,
        )

        # Mid drift in non-rolling mode produces actions (GRID_SHIFT / extra / missing)
        assert len(result.actions) > 0


class TestRollingGridEdgeCases:
    """Additional edge case and migration tests."""

    def test_rolling_init_auto_on_first_plan(self) -> None:
        """First plan(rolling_mode=True) auto-inits rolling state from mid_price."""
        planner = _make_planner(levels=3)

        # No manual init — plan() should auto-init
        assert planner.get_rolling_state(SYMBOL) is None

        result = planner.plan(
            symbol=SYMBOL,
            mid_price=ANCHOR,
            ts_ms=1000000,
            open_orders=(),
            rolling_mode=True,
        )

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.anchor_price == ANCHOR
        assert st.step_price == STEP
        assert st.net_offset == 0
        # All levels are missing → GRID_FILL
        assert result.desired_count == 6
        assert result.diff_missing == 6

    def test_rolling_step_price_never_zero(self) -> None:
        """If anchor * spacing rounds to 0, step_price = tick_size."""
        planner = LiveGridPlannerV1(
            _make_config(
                levels=3,
                spacing_bps=0.001,  # very small spacing
                tick_size="10.0",  # large tick
                size="0.01",
            )
        )

        # 100 * 0.001 / 10000 = 0.00001 → rounds to 0 → should use tick_size
        planner.init_rolling_state(SYMBOL, Decimal("100"), 0.001)

        st = planner.get_rolling_state(SYMBOL)
        assert st is not None
        assert st.step_price == Decimal("10.0"), f"Expected tick_size fallback, got {st.step_price}"

    def test_price_match_duplicate_prices(self) -> None:
        """Two exchange orders near same price: closest matched, other extra."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        ec = ANCHOR
        target_price = ec - STEP  # desired BUY L1

        orders = (
            # Exact match
            _make_order("grinder_d_BTCUSDT_1_1000000_1", SYMBOL, "BUY", str(target_price)),
            # Duplicate at same price (different order_id)
            _make_order("grinder_d_BTCUSDT_2_1000000_2", SYMBOL, "BUY", str(target_price)),
        )

        desired = planner._build_rolling_grid(SYMBOL)
        diff = planner._match_orders_by_price(orders, desired, ANCHOR)

        # One matched, one extra
        assert len(diff.extra_orders) >= 1, (
            f"Expected at least 1 extra, got {len(diff.extra_orders)}"
        )

    def test_price_match_no_double_match(self) -> None:
        """Each exchange order matches at most one desired level."""
        planner = _make_planner(levels=3)
        planner.init_rolling_state(SYMBOL, ANCHOR, SPACING_BPS)

        ec = ANCHOR
        # One order that could potentially match multiple levels
        orders = (_make_order("grinder_d_BTCUSDT_1_1000000_1", SYMBOL, "BUY", str(ec - STEP)),)

        desired = planner._build_rolling_grid(SYMBOL)
        diff = planner._match_orders_by_price(orders, desired, ANCHOR)

        # Should match exactly one desired level
        assert len(diff.matched_keys) == 1
        # Remaining 2 BUY levels + 3 SELL levels = 5 missing
        assert len(diff.missing_keys) == 5
