"""Unit tests for LiveGridPlannerV1 (doc-25).

Tests cover acceptance criteria from docs/25_LIVE_GRID_PLANNER_SPEC.md:
- AC1: Price shift triggers rebalance (cancel + place)
- AC2: Fill/expire triggers replenishment (missing → place)
- AC3: No churn below threshold (hysteresis)
- AC4: NATR fallback (adaptive_enabled but natr=None → static)
- AC5: Non-grinder orders ignored (foreign clientOrderId)
- Fail-safe: No tick_size → zero actions
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from grinder.account.contracts import OpenOrderSnap
from grinder.core import OrderSide
from grinder.execution.types import ActionType
from grinder.live.grid_planner import LiveGridConfig, LiveGridPlannerV1

# --- Helpers ---


def _make_order(
    *,
    side: str = "BUY",
    level_id: int = 1,
    price: Decimal = Decimal("50000"),
    qty: Decimal = Decimal("0.01"),
    symbol: str = "BTCUSDT",
    client_order_id: str | None = None,
    ts: int = 1000,
) -> OpenOrderSnap:
    """Create an OpenOrderSnap with a grinder_ clientOrderId."""
    if client_order_id is None:
        client_order_id = f"grinder_d_{symbol}_{level_id}_{ts}_1"
    return OpenOrderSnap(
        order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=price,
        qty=qty,
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=ts,
    )


def _make_config(
    *,
    levels: int = 5,
    spacing_bps: float = 10.0,
    tick_size: Decimal = Decimal("0.10"),
    size_per_level: Decimal = Decimal("0.01"),
    rebalance_threshold_steps: float = 1.0,
    adaptive_enabled: bool = False,
) -> LiveGridConfig:
    """Create a LiveGridConfig with sensible defaults."""
    return LiveGridConfig(
        base_spacing_bps=spacing_bps,
        levels=levels,
        size_per_level=size_per_level,
        tick_size=tick_size,
        rebalance_threshold_steps=rebalance_threshold_steps,
        adaptive_enabled=adaptive_enabled,
    )


def _build_matching_orders(
    mid_price: Decimal,
    config: LiveGridConfig,
    skip_levels: set[tuple[str, int]] | None = None,
) -> tuple[OpenOrderSnap, ...]:
    """Build exchange orders that exactly match the desired grid.

    Args:
        mid_price: Grid center price.
        config: Grid config (levels, spacing, tick_size, size_per_level).
        skip_levels: Set of (side, level_id) tuples to omit (simulates fills/expires).
    """
    if skip_levels is None:
        skip_levels = set()
    tick_size = config.tick_size or Decimal("0.10")
    spacing_factor = Decimal(str(config.base_spacing_bps)) / Decimal("10000")
    orders: list[OpenOrderSnap] = []

    for i in range(1, config.levels + 1):
        for side_str, sign in [("BUY", -1), ("SELL", 1)]:
            if (side_str, i) in skip_levels:
                continue
            raw_price = mid_price * (Decimal("1") + spacing_factor * i * sign)
            price = (raw_price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size
            orders.append(
                _make_order(
                    side=side_str,
                    level_id=i,
                    price=price,
                    qty=config.size_per_level,
                )
            )

    return tuple(orders)


# --- AC2: Missing orders → PLACE ---


class TestMissingOrdersPlace:
    """AC2: Fill/expire triggers replenishment."""

    def test_empty_exchange_places_all(self) -> None:
        """Empty exchange → place all desired levels."""
        config = _make_config(levels=5)
        planner = LiveGridPlannerV1(config)

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=1000,
            open_orders=(),
        )

        assert result.desired_count == 10  # 5 buy + 5 sell
        assert result.diff_missing == 10
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 10
        # All have GRID_FILL reason
        assert all(a.reason == "GRID_FILL" for a in place_actions)

    def test_partial_fill_places_missing(self) -> None:
        """8 of 10 orders present → 2 PLACE for missing levels."""
        config = _make_config(levels=5)
        planner = LiveGridPlannerV1(config)

        # Build 8 matching orders (skip BUY:L5 and SELL:L5)
        orders = _build_matching_orders(
            Decimal("50000"), config, skip_levels={("BUY", 5), ("SELL", 5)}
        )

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=1000,
            open_orders=orders,
        )

        assert result.diff_missing == 2
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2
        placed_sides = {(a.side, a.level_id) for a in place_actions}
        assert (OrderSide.BUY, 5) in placed_sides
        assert (OrderSide.SELL, 5) in placed_sides


# --- AC5: Foreign orders ignored ---


class TestForeignOrdersIgnored:
    """AC5: Non-grinder orders are not touched."""

    def test_foreign_orders_not_cancelled(self) -> None:
        """Foreign clientOrderId → not in diff, not cancelled."""
        config = _make_config(levels=2)
        planner = LiveGridPlannerV1(config)

        foreign_orders = (
            _make_order(client_order_id="binance_manual_123", level_id=1, side="BUY"),
            _make_order(client_order_id="other_bot_456", level_id=2, side="SELL"),
        )

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=1000,
            open_orders=foreign_orders,
        )

        # Foreign orders ignored — all desired levels are "missing"
        assert result.diff_extra == 0
        cancel_actions = [a for a in result.actions if a.action_type == ActionType.CANCEL]
        assert len(cancel_actions) == 0


# --- AC3: No churn below threshold ---


class TestHysteresis:
    """AC3: No churn below threshold."""

    def test_small_shift_no_actions(self) -> None:
        """Mid moves < 1 step with full grid → zero actions."""
        config = _make_config(levels=5, spacing_bps=10.0)
        planner = LiveGridPlannerV1(config)

        mid = Decimal("50000")
        orders = _build_matching_orders(mid, config)

        # First call: establishes center
        result1 = planner.plan(symbol="BTCUSDT", mid_price=mid, ts_ms=1000, open_orders=orders)
        assert result1.desired_count == 10

        # Second call: small shift (0.2 steps = 0.002% of 50000 = $1.00)
        # spacing = 10 bps = 0.1% = $50. 0.2 steps = $10.
        small_shift = mid + Decimal("10")
        # Rebuild orders to match new center (so no missing/extra)
        orders2 = _build_matching_orders(small_shift, config)

        result2 = planner.plan(
            symbol="BTCUSDT", mid_price=small_shift, ts_ms=2000, open_orders=orders2
        )

        assert result2.actions == []

    def test_missing_orders_override_hysteresis(self) -> None:
        """Small shift BUT missing orders → rebalance happens."""
        config = _make_config(levels=5, spacing_bps=10.0)
        planner = LiveGridPlannerV1(config)

        mid = Decimal("50000")
        orders = _build_matching_orders(mid, config)

        # Establish center
        planner.plan(symbol="BTCUSDT", mid_price=mid, ts_ms=1000, open_orders=orders)

        # Small shift + 2 missing orders
        small_shift = mid + Decimal("10")
        orders2 = _build_matching_orders(small_shift, config, skip_levels={("BUY", 5), ("SELL", 5)})

        result = planner.plan(
            symbol="BTCUSDT", mid_price=small_shift, ts_ms=2000, open_orders=orders2
        )

        # Missing orders force rebalance despite small shift
        assert result.diff_missing == 2
        place_actions = [a for a in result.actions if a.action_type == ActionType.PLACE]
        assert len(place_actions) == 2


# --- AC1: Price shift triggers cancel+place ---


class TestPriceShiftRebalance:
    """AC1: Price shift > threshold triggers rebalance."""

    def test_large_shift_triggers_actions(self) -> None:
        """Mid moves > 1 step with mismatched orders → cancel + place."""
        config = _make_config(levels=3, spacing_bps=10.0)
        planner = LiveGridPlannerV1(config)

        old_mid = Decimal("50000")
        old_orders = _build_matching_orders(old_mid, config)

        # Establish center
        planner.plan(symbol="BTCUSDT", mid_price=old_mid, ts_ms=1000, open_orders=old_orders)

        # Large shift: > 1 step = > 10 bps of 50000 = > $50
        new_mid = Decimal("50100")

        result = planner.plan(
            symbol="BTCUSDT", mid_price=new_mid, ts_ms=2000, open_orders=old_orders
        )

        # Old orders are now mismatched — expect cancels + places
        assert len(result.actions) > 0
        cancel_count = sum(1 for a in result.actions if a.action_type == ActionType.CANCEL)
        place_count = sum(1 for a in result.actions if a.action_type == ActionType.PLACE)
        assert cancel_count > 0
        assert place_count > 0


# --- AC4: NATR fallback ---


class TestNatrFallback:
    """AC4: NATR unavailable → static fallback."""

    def test_natr_none_uses_static(self) -> None:
        """adaptive_enabled=True but natr_bps=None → fallback to base_spacing_bps."""
        config = _make_config(levels=2, spacing_bps=15.0, adaptive_enabled=True)
        planner = LiveGridPlannerV1(config)

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=1000,
            open_orders=(),
            natr_bps=None,
        )

        assert result.natr_fallback is True
        assert result.effective_spacing_bps == 15.0

    def test_natr_stale_uses_static(self) -> None:
        """adaptive_enabled=True but natr is stale → fallback."""
        config = _make_config(levels=2, spacing_bps=15.0, adaptive_enabled=True)
        planner = LiveGridPlannerV1(config)

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=200_000,
            open_orders=(),
            natr_bps=30,
            natr_last_ts=0,  # 200s ago, > 120s stale threshold
        )

        assert result.natr_fallback is True
        assert result.effective_spacing_bps == 15.0


# --- Fail-safe: no tick_size ---


class TestFailSafe:
    """Fail-safe: no constraints → zero actions."""

    def test_no_tick_size_zero_actions(self) -> None:
        """tick_size=None → empty actions, desired_count=0."""
        config = LiveGridConfig(tick_size=None)
        planner = LiveGridPlannerV1(config)

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=1000,
            open_orders=(),
        )

        assert result.actions == []
        assert result.desired_count == 0


# --- PR-INV-2: suppress_increase (cancel-only mode) ---


class TestSuppressIncrease:
    """PR-INV-2: suppress_increase filters out PLACE/REPLACE."""

    def test_suppress_removes_place_keeps_cancel(self) -> None:
        """suppress_increase=True: PLACE filtered out, CANCEL kept."""
        config = _make_config(levels=3, spacing_bps=10.0)
        planner = LiveGridPlannerV1(config)

        old_mid = Decimal("50000")
        old_orders = _build_matching_orders(old_mid, config)

        # Establish center
        planner.plan(symbol="BTCUSDT", mid_price=old_mid, ts_ms=1000, open_orders=old_orders)

        # Large shift: old orders mismatch -> normally produces CANCEL + PLACE
        new_mid = Decimal("50100")
        result_normal = planner.plan(
            symbol="BTCUSDT",
            mid_price=new_mid,
            ts_ms=2000,
            open_orders=old_orders,
            suppress_increase=False,
        )
        normal_places = [a for a in result_normal.actions if a.action_type == ActionType.PLACE]
        normal_cancels = [a for a in result_normal.actions if a.action_type == ActionType.CANCEL]
        assert len(normal_places) > 0, "Normal mode should produce PLACE actions"
        assert len(normal_cancels) > 0, "Normal mode should produce CANCEL actions"

        # Reset planner center for fair comparison
        planner2 = LiveGridPlannerV1(config)
        planner2.plan(symbol="BTCUSDT", mid_price=old_mid, ts_ms=1000, open_orders=old_orders)

        # suppress_increase=True: only CANCEL survives
        result_suppressed = planner2.plan(
            symbol="BTCUSDT",
            mid_price=new_mid,
            ts_ms=2000,
            open_orders=old_orders,
            suppress_increase=True,
        )
        suppressed_places = [
            a for a in result_suppressed.actions if a.action_type == ActionType.PLACE
        ]
        suppressed_cancels = [
            a for a in result_suppressed.actions if a.action_type == ActionType.CANCEL
        ]
        assert len(suppressed_places) == 0, "suppress_increase must filter out all PLACE"
        assert len(suppressed_cancels) == len(normal_cancels), "CANCEL actions must be preserved"

    def test_suppress_empty_exchange_no_place(self) -> None:
        """suppress_increase=True + empty exchange -> zero actions (no PLACE)."""
        config = _make_config(levels=3)
        planner = LiveGridPlannerV1(config)

        result = planner.plan(
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            ts_ms=1000,
            open_orders=(),
            suppress_increase=True,
        )

        # Normally would produce 6 PLACE (3 buy + 3 sell), but all suppressed
        assert result.desired_count == 6
        assert result.diff_missing == 6
        assert len(result.actions) == 0
