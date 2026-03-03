"""LiveGridPlannerV1: exchange-truth grid reconciliation engine (doc-25).

Replaces PaperEngine as the source of order decisions in live/mainnet mode.
Uses exchange open orders (via AccountSync) as source of truth, diffs against
a computed desired grid, and emits PLACE / CANCEL actions.

Core invariants (doc-25 SS 25.6):
- I1: Exchange is source of truth for open orders (no ghost state)
- I2: effective_spacing_bps >= step_min_bps always
- I3: No actions if shift < threshold AND no missing/extra
- I5: clientOrderId scheme used for level matching
- I6: Orders not matching our clientOrderId prefix are ignored
- I7: Read-only: planner never calls exchange directly
- I8: Planner is stateless per call (except last_plan_center cache)

See: docs/25_LIVE_GRID_PLANNER_SPEC.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from grinder.controller.regime import Regime
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.policies.grid.adaptive import AdaptiveGridConfig, compute_step_bps
from grinder.reconcile.identity import DEFAULT_PREFIX, parse_client_order_id

if TYPE_CHECKING:
    from grinder.account.contracts import OpenOrderSnap

logger = logging.getLogger(__name__)


@dataclass
class LiveGridConfig:
    """Configuration for LiveGridPlannerV1 (doc-25 SS 25.3).

    Attributes:
        base_spacing_bps: Static grid spacing (used as-is or as adaptive base).
        levels: Levels per side (total = 2 * levels).
        size_per_level: Order quantity per level (base asset).
        adaptive_enabled: Use NATR-driven spacing.
        step_alpha: NATR multiplier (scaled /100).
        step_min_bps: Spacing floor.
        natr_stale_ms: NATR staleness threshold (2 * bar_interval).
        rebalance_threshold_steps: Min grid shift (in steps) to trigger rebalance.
        price_epsilon_bps: Price match tolerance for diff.
        qty_epsilon_pct: Qty match tolerance (%).
        tick_size: Price rounding increment (from exchange constraints).
            None = no constraints available (fail-safe: zero actions).
    """

    base_spacing_bps: float = 10.0
    levels: int = 5
    size_per_level: Decimal = field(default_factory=lambda: Decimal("0.01"))
    adaptive_enabled: bool = False
    step_alpha: int = 30
    step_min_bps: int = 5
    natr_stale_ms: int = 120_000
    rebalance_threshold_steps: float = 1.0
    price_epsilon_bps: float = 0.5
    qty_epsilon_pct: float = 1.0
    tick_size: Decimal | None = None


@dataclass(frozen=True)
class GridPlanResult:
    """Output of LiveGridPlannerV1.plan() (doc-25 SS 25.4).

    Attributes:
        actions: PLACE / CANCEL actions.
        desired_count: Number of desired grid levels.
        actual_count: Number of matched exchange orders.
        diff_missing: Desired but not on exchange.
        diff_extra: On exchange but not in desired.
        diff_mismatch: Matched but price/qty differs.
        effective_spacing_bps: Actual spacing used.
        natr_fallback: True if NATR unavailable, using static.
    """

    actions: list[ExecutionAction] = field(default_factory=list)
    desired_count: int = 0
    actual_count: int = 0
    diff_missing: int = 0
    diff_extra: int = 0
    diff_mismatch: int = 0
    effective_spacing_bps: float = 0.0
    natr_fallback: bool = False


@dataclass
class _DesiredLevel:
    """Internal: one level of the desired grid."""

    key: str  # e.g., "BUY:L1", "SELL:L3"
    side: OrderSide
    level_id: int
    price: Decimal


@dataclass
class _DiffResult:
    """Internal: result of matching exchange orders to desired grid."""

    matched_keys: set[str]
    extra_orders: list[OpenOrderSnap]
    mismatch_orders: list[tuple[OpenOrderSnap, _DesiredLevel]]
    missing_keys: list[str]


def _round_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round price DOWN to nearest tick increment."""
    if tick_size <= 0:
        return price
    return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size


class LiveGridPlannerV1:
    """Exchange-truth grid reconciliation planner (doc-25).

    Stateless per call except for hysteresis cache (last_plan_center).
    Never calls exchange directly — actions go through LiveEngine pipeline.
    """

    def __init__(self, config: LiveGridConfig) -> None:
        self._config = config
        # Hysteresis cache: per-symbol last plan center + timestamp
        self._last_plan_center: dict[str, Decimal] = {}
        self._last_plan_ts_ms: dict[str, int] = {}

    def plan(
        self,
        *,
        symbol: str,
        mid_price: Decimal,
        ts_ms: int,
        open_orders: tuple[OpenOrderSnap, ...],
        natr_bps: int | None = None,
        natr_last_ts: int = 0,
        regime: Regime = Regime.RANGE,
    ) -> GridPlanResult:
        """Compute grid plan by diffing desired vs exchange orders.

        Args:
            symbol: Trading pair.
            mid_price: Current mid-price.
            ts_ms: Epoch milliseconds.
            open_orders: Exchange open orders (from AccountSync).
            natr_bps: NATR(14) in integer bps (None = unavailable).
            natr_last_ts: Timestamp of last NATR computation.
            regime: Market regime (default RANGE).

        Returns:
            GridPlanResult with actions and diff statistics.
        """
        cfg = self._config

        # Step A: Compute effective spacing (I2: floor holds)
        effective_spacing_bps, natr_fallback = self._compute_spacing(
            natr_bps, natr_last_ts, ts_ms, regime
        )

        # Step B: Build desired grid (fail-safe: no tick_size → zero actions)
        if cfg.tick_size is None or cfg.tick_size <= 0:
            logger.warning("No tick_size for %s, skipping grid plan", symbol)
            return GridPlanResult(
                effective_spacing_bps=effective_spacing_bps,
                natr_fallback=natr_fallback,
            )

        desired = self._build_desired_grid(mid_price, effective_spacing_bps, cfg.tick_size)

        # Step C+D: Match exchange orders to desired levels
        diff = self._match_orders(open_orders, desired, mid_price)

        # Step E: Hysteresis (anti-churn, I3)
        if self._should_skip_rebalance(symbol, mid_price, effective_spacing_bps, diff):
            return GridPlanResult(
                desired_count=len(desired),
                actual_count=len(diff.matched_keys),
                effective_spacing_bps=effective_spacing_bps,
                natr_fallback=natr_fallback,
            )

        # Step F: Generate actions
        actions = self._generate_actions(symbol, diff, desired, mid_price)

        # Update hysteresis cache
        self._last_plan_center[symbol] = mid_price
        self._last_plan_ts_ms[symbol] = ts_ms

        return GridPlanResult(
            actions=actions,
            desired_count=len(desired),
            actual_count=len(diff.matched_keys) - len(diff.mismatch_orders),
            diff_missing=len(diff.missing_keys),
            diff_extra=len(diff.extra_orders),
            diff_mismatch=len(diff.mismatch_orders),
            effective_spacing_bps=effective_spacing_bps,
            natr_fallback=natr_fallback,
        )

    def _compute_spacing(
        self,
        natr_bps: int | None,
        natr_last_ts: int,
        ts_ms: int,
        regime: Regime,
    ) -> tuple[float, bool]:
        """Step A: Compute effective spacing with NATR fallback (doc-25 SS 25.5)."""
        cfg = self._config
        natr_fallback = True
        effective = cfg.base_spacing_bps

        if (
            cfg.adaptive_enabled
            and natr_bps is not None
            and natr_bps > 0
            and (ts_ms - natr_last_ts) <= cfg.natr_stale_ms
        ):
            adaptive_cfg = AdaptiveGridConfig(
                step_alpha=cfg.step_alpha,
                step_min_bps=cfg.step_min_bps,
            )
            raw_step = compute_step_bps(natr_bps, regime, adaptive_cfg)
            effective = float(max(cfg.step_min_bps, raw_step))
            natr_fallback = False

        # Invariant I2: floor holds
        return max(effective, float(cfg.step_min_bps)), natr_fallback

    def _match_orders(
        self,
        open_orders: tuple[OpenOrderSnap, ...],
        desired: list[_DesiredLevel],
        mid_price: Decimal,
    ) -> _DiffResult:
        """Step C+D: Match exchange orders to desired grid levels (doc-25 SS 25.5)."""
        cfg = self._config
        desired_by_key = {d.key: d for d in desired}
        matched_keys: set[str] = set()
        extra_orders: list[OpenOrderSnap] = []
        mismatch_orders: list[tuple[OpenOrderSnap, _DesiredLevel]] = []

        for order in open_orders:
            # Step C: Filter foreign orders (I6)
            parsed = parse_client_order_id(order.order_id)
            if parsed is None or not parsed.prefix.startswith(DEFAULT_PREFIX.rstrip("_")):
                continue

            # Build match key from parsed order
            level_key = f"{order.side.upper()}:L{parsed.level_id}"

            if level_key in desired_by_key and level_key not in matched_keys:
                desired_level = desired_by_key[level_key]
                price_diff = float(abs(order.price - desired_level.price) / mid_price) * 10000
                qty_diff = (
                    float(abs(order.qty - cfg.size_per_level) / cfg.size_per_level) * 100
                    if cfg.size_per_level > 0
                    else 0.0
                )
                if price_diff > cfg.price_epsilon_bps or qty_diff > cfg.qty_epsilon_pct:
                    mismatch_orders.append((order, desired_level))
                matched_keys.add(level_key)
            else:
                extra_orders.append(order)

        missing_keys = [k for k in desired_by_key if k not in matched_keys]
        return _DiffResult(
            matched_keys=matched_keys,
            extra_orders=extra_orders,
            mismatch_orders=mismatch_orders,
            missing_keys=missing_keys,
        )

    def _should_skip_rebalance(
        self,
        symbol: str,
        mid_price: Decimal,
        effective_spacing_bps: float,
        diff: _DiffResult,
    ) -> bool:
        """Step E: Check hysteresis — skip rebalance if shift < threshold (I3)."""
        # If there are diffs, always rebalance (fills/expires removed orders)
        if diff.missing_keys or diff.extra_orders or diff.mismatch_orders:
            return False

        last_center = self._last_plan_center.get(symbol)
        if last_center is None or effective_spacing_bps <= 0:
            return False

        step_abs = mid_price * Decimal(str(effective_spacing_bps)) / Decimal("10000")
        shift_steps = float(abs(mid_price - last_center) / step_abs) if step_abs > 0 else 0.0
        return shift_steps < self._config.rebalance_threshold_steps

    def _generate_actions(
        self,
        symbol: str,
        diff: _DiffResult,
        desired: list[_DesiredLevel],
        mid_price: Decimal,
    ) -> list[ExecutionAction]:
        """Step F: Generate PLACE/CANCEL actions from diff (doc-25 SS 25.5)."""
        cfg = self._config
        desired_by_key = {d.key: d for d in desired}
        actions: list[ExecutionAction] = []

        # Missing → PLACE
        for key in diff.missing_keys:
            level = desired_by_key[key]
            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=level.side,
                    price=level.price,
                    quantity=cfg.size_per_level,
                    level_id=level.level_id,
                    reason="GRID_FILL",
                )
            )

        # Extra → CANCEL
        for order in diff.extra_orders:
            actions.append(
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    order_id=order.order_id,
                    symbol=symbol,
                    reason="GRID_TRIM",
                )
            )

        # Mismatch → CANCEL + PLACE
        for order, desired_level in diff.mismatch_orders:
            price_diff = float(abs(order.price - desired_level.price) / mid_price) * 10000
            reason = "GRID_SHIFT" if price_diff > cfg.price_epsilon_bps else "GRID_RESIZE"
            actions.append(
                ExecutionAction(
                    action_type=ActionType.CANCEL,
                    order_id=order.order_id,
                    symbol=symbol,
                    reason=reason,
                )
            )
            actions.append(
                ExecutionAction(
                    action_type=ActionType.PLACE,
                    symbol=symbol,
                    side=desired_level.side,
                    price=desired_level.price,
                    quantity=cfg.size_per_level,
                    level_id=desired_level.level_id,
                    reason=reason,
                )
            )

        return actions

    def _build_desired_grid(
        self,
        mid_price: Decimal,
        spacing_bps: float,
        tick_size: Decimal,
    ) -> list[_DesiredLevel]:
        """Build symmetric bilateral grid centered on mid_price (doc-25 SS 25.5 Step 2)."""
        levels: list[_DesiredLevel] = []
        spacing_factor = Decimal(str(spacing_bps)) / Decimal("10000")

        for i in range(1, self._config.levels + 1):
            buy_price = _round_to_tick(
                mid_price * (Decimal("1") - spacing_factor * i),
                tick_size,
            )
            sell_price = _round_to_tick(
                mid_price * (Decimal("1") + spacing_factor * i),
                tick_size,
            )
            levels.append(
                _DesiredLevel(key=f"BUY:L{i}", side=OrderSide.BUY, level_id=i, price=buy_price)
            )
            levels.append(
                _DesiredLevel(key=f"SELL:L{i}", side=OrderSide.SELL, level_id=i, price=sell_price)
            )

        return levels
