"""Paper trading engine with gating controls.

This module provides a paper trading loop that:
1. Loads fixture events (Snapshots)
2. Applies prefilter gates
3. Applies gating controls (rate limit, risk limits)
4. Evaluates policy to get GridPlan
5. Executes via ExecutionEngine (no real orders)
6. Simulates fills and tracks positions/PnL
7. Produces deterministic output digest

See: docs/10_RISK_SPEC.md for gating limits
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path  # noqa: TC003 - used at runtime
from typing import Any

from grinder.contracts import Snapshot
from grinder.controller import AdaptiveController, ControllerMode
from grinder.execution import ExecutionEngine, ExecutionState, NoOpExchangePort
from grinder.features import FeatureEngine, FeatureEngineConfig
from grinder.gating import GateReason, GatingResult, RateLimiter, RiskGate, ToxicityGate
from grinder.paper.cycle_engine import CycleEngine
from grinder.paper.fills import simulate_fills
from grinder.paper.ledger import Ledger
from grinder.policies.base import GridPlan  # noqa: TC001 - used at runtime
from grinder.policies.grid.static import StaticGridPolicy
from grinder.prefilter import TopKSelector, hard_filter
from grinder.risk import DrawdownGuard, KillSwitch, KillSwitchReason

# Output schema version for contract stability
SCHEMA_VERSION = "v1"


@dataclass
class PaperOutput:
    """Single paper trading cycle output.

    Schema v1 contract - adding new fields is allowed, removing/renaming is breaking.
    """

    ts: int
    symbol: str
    prefilter_result: dict[str, Any]
    gating_result: dict[str, Any]
    plan: dict[str, Any] | None
    actions: list[dict[str, Any]]
    events: list[dict[str, Any]]
    blocked_by_gating: bool
    # v1 additions: fills and PnL
    fills: list[dict[str, Any]] = field(default_factory=list)
    pnl_snapshot: dict[str, Any] | None = None
    # v1 additions: drawdown and kill-switch (ADR-013)
    # Note: These fields are NOT included in digest computation for backward compatibility.
    drawdown_check: dict[str, Any] | None = None
    kill_switch_triggered: bool = False
    # v1 additions: CycleEngine intents (ADR-017)
    # Note: NOT included in digest computation for backward compatibility.
    cycle_intents: list[dict[str, Any]] = field(default_factory=list)
    # v1 additions: FeatureEngine features (ADR-019)
    # Note: NOT included in digest computation for backward compatibility.
    features: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "prefilter_result": self.prefilter_result,
            "gating_result": self.gating_result,
            "plan": self.plan,
            "actions": self.actions,
            "events": self.events,
            "blocked_by_gating": self.blocked_by_gating,
            "fills": self.fills,
            "pnl_snapshot": self.pnl_snapshot,
            "drawdown_check": self.drawdown_check,
            "kill_switch_triggered": self.kill_switch_triggered,
            "cycle_intents": self.cycle_intents,
            "features": self.features,
        }

    def to_digest_dict(self) -> dict[str, Any]:
        """Convert to dict for digest computation (excludes new fields for backward compat)."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "prefilter_result": self.prefilter_result,
            "gating_result": self.gating_result,
            "plan": self.plan,
            "actions": self.actions,
            "events": self.events,
            "blocked_by_gating": self.blocked_by_gating,
            "fills": self.fills,
            "pnl_snapshot": self.pnl_snapshot,
        }


@dataclass
class PaperResult:
    """Complete paper trading result.

    Schema v1 contract - adding new fields is allowed, removing/renaming is breaking.
    """

    fixture_path: str
    outputs: list[PaperOutput] = field(default_factory=list)
    digest: str = ""
    events_processed: int = 0
    events_gated: int = 0
    orders_placed: int = 0
    orders_blocked: int = 0
    errors: list[str] = field(default_factory=list)
    # v1 additions
    schema_version: str = SCHEMA_VERSION
    total_fills: int = 0
    final_positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_realized_pnl: str = "0"
    total_unrealized_pnl: str = "0"
    # Top-K prefilter results (v1 addition - ADR-010)
    # Note: These fields are NOT included in digest computation to preserve
    # backward compatibility with existing canonical digests.
    topk_selected_symbols: list[str] = field(default_factory=list)
    topk_k: int = 0
    topk_scores: list[dict[str, Any]] = field(default_factory=list)
    # Controller results (v1 addition - ADR-011)
    # Note: These fields are NOT included in digest computation.
    # Controller is opt-in; when disabled, these fields are empty/defaults.
    controller_enabled: bool = False
    controller_decisions: list[dict[str, Any]] = field(default_factory=list)
    # Kill-switch results (v1 addition - ADR-013)
    # Note: These fields are NOT included in digest computation.
    kill_switch_enabled: bool = False
    kill_switch_triggered: bool = False
    kill_switch_state: dict[str, Any] | None = None
    final_equity: str = "0"
    final_drawdown_pct: float = 0.0
    high_water_mark: str = "0"
    # FeatureEngine results (v1 addition - ADR-019)
    # Note: These fields are NOT included in digest computation.
    feature_engine_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "fixture_path": self.fixture_path,
            "outputs": [o.to_dict() for o in self.outputs],
            "digest": self.digest,
            "events_processed": self.events_processed,
            "events_gated": self.events_gated,
            "orders_placed": self.orders_placed,
            "orders_blocked": self.orders_blocked,
            "total_fills": self.total_fills,
            "final_positions": self.final_positions,
            "total_realized_pnl": self.total_realized_pnl,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "errors": self.errors,
            "topk_selected_symbols": self.topk_selected_symbols,
            "topk_k": self.topk_k,
            "topk_scores": self.topk_scores,
            "controller_enabled": self.controller_enabled,
            "controller_decisions": self.controller_decisions,
            "kill_switch_enabled": self.kill_switch_enabled,
            "kill_switch_triggered": self.kill_switch_triggered,
            "kill_switch_state": self.kill_switch_state,
            "final_equity": self.final_equity,
            "final_drawdown_pct": self.final_drawdown_pct,
            "high_water_mark": self.high_water_mark,
            "feature_engine_enabled": self.feature_engine_enabled,
        }

    def to_json(self) -> str:
        """Serialize to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class PaperEngine:
    """Paper trading engine with gating controls.

    Wires together: prefilter -> gating -> policy -> execution
    No real orders are placed - all execution is simulated via NoOpExchangePort.
    """

    def __init__(
        self,
        spacing_bps: float = 10.0,
        levels: int = 5,
        size_per_level: Decimal = Decimal("100"),
        price_precision: int = 2,
        quantity_precision: int = 3,
        max_orders_per_minute: int = 60,
        cooldown_ms: int = 100,
        max_notional_per_symbol: Decimal = Decimal("5000"),
        max_notional_total: Decimal = Decimal("20000"),
        daily_loss_limit: Decimal = Decimal("500"),
        max_spread_bps: float = 50.0,
        max_price_impact_bps: float = 500.0,  # 5% - high to avoid triggering on normal volatility
        toxicity_lookback_ms: int = 5000,
        topk_k: int = 3,
        topk_window_size: int = 10,
        # Adaptive Controller parameters (ADR-011)
        controller_enabled: bool = False,
        controller_window_size: int = 10,
        controller_spread_pause_bps: int = 50,
        controller_vol_widen_bps: int = 300,
        controller_vol_tighten_bps: int = 50,
        controller_widen_multiplier: float = 1.5,
        controller_tighten_multiplier: float = 0.8,
        # Kill-switch and drawdown guard parameters (ADR-013)
        initial_capital: Decimal = Decimal("10000"),
        max_drawdown_pct: float = 5.0,
        kill_switch_enabled: bool = False,
        # Fill simulation mode (ADR-016)
        fill_mode: str = "crossing",
        # CycleEngine parameters (ADR-017)
        cycle_enabled: bool = False,
        cycle_step_pct: Decimal = Decimal("0.001"),  # 10 bps default
        # FeatureEngine parameters (ADR-019)
        feature_engine_enabled: bool = False,
        feature_bar_interval_ms: int = 60_000,
        feature_atr_period: int = 14,
        feature_range_horizon: int = 14,
        feature_max_bars: int = 1000,
    ) -> None:
        """Initialize paper trading engine.

        Args:
            spacing_bps: Grid spacing in basis points
            levels: Number of levels on each side
            size_per_level: Order size per level
            price_precision: Decimal places for price
            quantity_precision: Decimal places for quantity
            max_orders_per_minute: Rate limit for orders
            cooldown_ms: Minimum ms between orders
            max_notional_per_symbol: Max notional per symbol (USD)
            max_notional_total: Max total notional (USD)
            daily_loss_limit: Max daily loss before blocking (USD)
            max_spread_bps: Max spread in bps before toxicity block
            max_price_impact_bps: Max price change in bps before toxicity block
            toxicity_lookback_ms: Lookback window for price impact calculation
            topk_k: Number of top symbols to select (default 3)
            topk_window_size: Window size for volatility scoring (default 10)
            controller_enabled: Enable adaptive controller (default False for backward compatibility)
            controller_window_size: Window size for controller metrics (default 10)
            controller_spread_pause_bps: Spread threshold for PAUSE mode (default 50)
            controller_vol_widen_bps: Volatility threshold for WIDEN mode (default 300)
            controller_vol_tighten_bps: Volatility threshold for TIGHTEN mode (default 50)
            controller_widen_multiplier: Spacing multiplier for WIDEN mode (default 1.5)
            controller_tighten_multiplier: Spacing multiplier for TIGHTEN mode (default 0.8)
            initial_capital: Starting capital for equity calculation (default 10000 USD)
            max_drawdown_pct: Maximum drawdown percentage before kill-switch (default 5.0%)
            kill_switch_enabled: Enable drawdown guard and kill-switch (default False for backward compat)
            fill_mode: Fill simulation mode - "crossing" (default, v1.1) or "instant" (v1.0 compat)
            cycle_enabled: Enable CycleEngine for TP + replenishment (default False for backward compat)
            cycle_step_pct: Step percentage for TP/replenish placement (default 0.001 = 10 bps)
            feature_engine_enabled: Enable FeatureEngine for L1/volatility features (default False)
            feature_bar_interval_ms: Bar interval for feature computation (default 60_000 = 1m)
            feature_atr_period: Period for ATR/NATR calculation (default 14)
            feature_range_horizon: Horizon for range/trend calculation (default 14)
            feature_max_bars: Maximum bars to keep per symbol (default 1000)
        """
        # Policy and execution
        self._policy = StaticGridPolicy(
            spacing_bps=spacing_bps,
            levels=levels,
            size_per_level=size_per_level,
        )
        self._port = NoOpExchangePort()
        self._engine = ExecutionEngine(
            port=self._port,
            price_precision=price_precision,
            quantity_precision=quantity_precision,
        )

        # Gating controls
        self._rate_limiter = RateLimiter(
            max_orders_per_minute=max_orders_per_minute,
            cooldown_ms=cooldown_ms,
        )
        self._risk_gate = RiskGate(
            max_notional_per_symbol=max_notional_per_symbol,
            max_notional_total=max_notional_total,
            daily_loss_limit=daily_loss_limit,
        )
        self._toxicity_gate = ToxicityGate(
            max_spread_bps=max_spread_bps,
            max_price_impact_bps=max_price_impact_bps,
            lookback_window_ms=toxicity_lookback_ms,
        )

        # Top-K symbol selector (ADR-010)
        self._topk_selector = TopKSelector(k=topk_k, window_size=topk_window_size)

        # Adaptive Controller (ADR-011)
        self._controller_enabled = controller_enabled
        self._controller = AdaptiveController(
            window_size=controller_window_size,
            spread_pause_bps=controller_spread_pause_bps,
            vol_widen_bps=controller_vol_widen_bps,
            vol_tighten_bps=controller_vol_tighten_bps,
            widen_multiplier=controller_widen_multiplier,
            tighten_multiplier=controller_tighten_multiplier,
        )
        self._base_spacing_bps = spacing_bps  # Store for controller adjustment

        # Kill-switch and drawdown guard (ADR-013)
        self._kill_switch_enabled = kill_switch_enabled
        self._initial_capital = initial_capital
        self._kill_switch = KillSwitch()
        self._drawdown_guard: DrawdownGuard | None = None
        if kill_switch_enabled:
            self._drawdown_guard = DrawdownGuard(
                initial_capital=initial_capital,
                max_drawdown_pct=max_drawdown_pct,
            )

        # Fill simulation mode (ADR-016)
        self._fill_mode = fill_mode

        # CycleEngine for TP + replenishment (ADR-017)
        self._cycle_enabled = cycle_enabled
        self._cycle_engine: CycleEngine | None = None
        if cycle_enabled:
            self._cycle_engine = CycleEngine(
                step_pct=cycle_step_pct,
                price_precision=price_precision,
                quantity_precision=quantity_precision,
            )

        # FeatureEngine for L1/volatility features (ADR-019)
        self._feature_engine_enabled = feature_engine_enabled
        self._feature_engine: FeatureEngine | None = None
        if feature_engine_enabled:
            self._feature_engine = FeatureEngine(
                config=FeatureEngineConfig(
                    bar_interval_ms=feature_bar_interval_ms,
                    atr_period=feature_atr_period,
                    range_horizon=feature_range_horizon,
                    max_bars=feature_max_bars,
                )
            )

        # Per-symbol execution state
        self._states: dict[str, ExecutionState] = {}

        # Position and PnL tracking
        self._ledger = Ledger()

        # Metrics
        self._orders_placed = 0
        self._orders_blocked = 0
        self._total_fills = 0

        # Last prices for unrealized PnL calculation
        self._last_prices: dict[str, Decimal] = {}

    def _get_state(self, symbol: str) -> ExecutionState:
        """Get or create execution state for symbol."""
        if symbol not in self._states:
            self._states[symbol] = ExecutionState()
        return self._states[symbol]

    def _plan_to_dict(self, plan: GridPlan) -> dict[str, Any]:
        """Convert GridPlan to JSON-serializable dict."""
        return {
            "mode": plan.mode.value,
            "center_price": str(plan.center_price),
            "spacing_bps": plan.spacing_bps,
            "levels_up": plan.levels_up,
            "levels_down": plan.levels_down,
            "size_schedule": [str(s) for s in plan.size_schedule],
            "skew_bps": plan.skew_bps,
            "regime": plan.regime.value,
            "width_bps": plan.width_bps,
            "reset_action": plan.reset_action.value,
            "reason_codes": plan.reason_codes,
        }

    def _check_gating(
        self,
        ts: int,
        symbol: str,
        proposed_notional: Decimal,
        spread_bps: float,
        mid_price: Decimal,
    ) -> GatingResult:
        """Check all gating controls.

        Args:
            ts: Timestamp in milliseconds
            symbol: Trading symbol
            proposed_notional: Estimated notional for proposed orders
            spread_bps: Current spread in basis points
            mid_price: Current mid price

        Returns:
            Combined GatingResult (first failure or allow)
        """
        # Check toxicity FIRST (market conditions)
        tox_result = self._toxicity_gate.check(ts, symbol, spread_bps, mid_price)
        if not tox_result.allowed:
            return tox_result

        # Check rate limit
        rate_result = self._rate_limiter.check(ts)
        if not rate_result.allowed:
            return rate_result

        # Check risk limits
        risk_result = self._risk_gate.check_order(symbol, proposed_notional)
        if not risk_result.allowed:
            return risk_result

        # Note: toxicity_gate details not included in allow result to preserve
        # backward compatibility with existing canonical digests (ADR-009)
        return GatingResult.allow(
            {
                "rate_limiter": rate_result.details,
                "risk_gate": risk_result.details,
            }
        )

    def _estimate_notional(self, plan: GridPlan, mid_price: Decimal) -> Decimal:
        """Estimate notional for one order (conservative).

        We estimate for a single order rather than the entire grid because:
        - Actual orders depend on reconciliation (may place 0, 1, or few)
        - Notional is tracked per-order after placement
        - This gate just ensures we have room for at least one more order
        """
        avg_size = plan.size_schedule[0] if plan.size_schedule else Decimal("1")
        return avg_size * mid_price

    def process_snapshot(self, snapshot: Snapshot) -> PaperOutput:
        """Process single snapshot through full pipeline with gating.

        Args:
            snapshot: Market data snapshot

        Returns:
            PaperOutput with prefilter, gating, plan, execution, fills, and PnL
        """
        symbol = snapshot.symbol
        ts = snapshot.ts

        # Track last price for unrealized PnL
        self._last_prices[symbol] = snapshot.mid_price

        # Step 0.5: Compute features (ADR-019)
        # Always compute features to build bar history, even if blocked later
        features_dict: dict[str, Any] | None = None
        policy_feature_inputs: dict[str, Any] | None = None
        if self._feature_engine_enabled and self._feature_engine is not None:
            feature_snapshot = self._feature_engine.process_snapshot(snapshot)
            features_dict = feature_snapshot.to_dict()
            policy_feature_inputs = feature_snapshot.to_policy_features()

        # Step 1: Prefilter
        features = {
            "spread_bps": snapshot.spread_bps,
            "vol_24h_usd": 100_000_000.0,  # Assume sufficient for paper
            "vol_1h_usd": 10_000_000.0,
        }
        filter_result = hard_filter(symbol, features)

        # Get PnL snapshot even if blocked (mark-to-market)
        pnl_snap = self._ledger.get_pnl_snapshot(ts, symbol, snapshot.mid_price)

        # Step 0: Kill-switch check (ADR-013)
        # If kill-switch is triggered, block all trading
        if self._kill_switch_enabled and self._kill_switch.is_triggered:
            return PaperOutput(
                ts=ts,
                symbol=symbol,
                prefilter_result=filter_result.to_dict(),
                gating_result=GatingResult.block(
                    GateReason.KILL_SWITCH_ACTIVE,
                    {"kill_switch_state": self._kill_switch.state.to_dict()},
                ).to_dict(),
                plan=None,
                actions=[],
                events=[],
                blocked_by_gating=True,
                fills=[],
                pnl_snapshot=pnl_snap.to_dict(),
                kill_switch_triggered=True,
                features=features_dict,
            )

        # If blocked by prefilter, return early
        if not filter_result.allowed:
            return PaperOutput(
                ts=ts,
                symbol=symbol,
                prefilter_result=filter_result.to_dict(),
                gating_result=GatingResult.allow().to_dict(),
                plan=None,
                actions=[],
                events=[],
                blocked_by_gating=False,
                fills=[],
                pnl_snapshot=pnl_snap.to_dict(),
                features=features_dict,
            )

        # Step 2: Controller decision (if enabled) and policy evaluation
        # Record for controller before decision
        if self._controller_enabled:
            self._controller.record(ts, symbol, snapshot.mid_price, snapshot.spread_bps)

        # Get controller decision
        controller_decision = None
        effective_spacing_bps = self._base_spacing_bps
        if self._controller_enabled:
            controller_decision = self._controller.decide(symbol)
            # If controller says PAUSE, block this event
            if controller_decision.mode == ControllerMode.PAUSE:
                self._orders_blocked += 1
                return PaperOutput(
                    ts=ts,
                    symbol=symbol,
                    prefilter_result=filter_result.to_dict(),
                    gating_result=GatingResult.allow().to_dict(),
                    plan=None,
                    actions=[],
                    events=[],
                    blocked_by_gating=True,  # Treat controller PAUSE like gating block
                    fills=[],
                    pnl_snapshot=pnl_snap.to_dict(),
                    features=features_dict,
                )
            # Apply spacing multiplier
            effective_spacing_bps = self._base_spacing_bps * controller_decision.spacing_multiplier

        # Policy evaluation with effective spacing
        # Base features always include mid_price
        policy_features: dict[str, Any] = {
            "mid_price": snapshot.mid_price,
        }
        # Merge FeatureEngine features when enabled (ADR-020)
        # StaticGridPolicy ignores extra keys; future policies can use them
        if policy_feature_inputs is not None:
            policy_features.update(policy_feature_inputs)
        # Create a temporary policy with adjusted spacing if controller is active
        if self._controller_enabled and effective_spacing_bps != self._base_spacing_bps:
            temp_policy = StaticGridPolicy(
                spacing_bps=effective_spacing_bps,
                levels=self._policy.levels,
                size_per_level=self._policy.size_per_level,
            )
            plan = temp_policy.evaluate(policy_features)
        else:
            plan = self._policy.evaluate(policy_features)

        # Step 3: Gating check (includes toxicity)
        # Record price for toxicity tracking before check
        self._toxicity_gate.record_price(ts, symbol, snapshot.mid_price)

        estimated_notional = self._estimate_notional(plan, snapshot.mid_price)
        gating_result = self._check_gating(
            ts, symbol, estimated_notional, snapshot.spread_bps, snapshot.mid_price
        )

        if not gating_result.allowed:
            self._orders_blocked += 1
            return PaperOutput(
                ts=ts,
                symbol=symbol,
                prefilter_result=filter_result.to_dict(),
                gating_result=gating_result.to_dict(),
                plan=self._plan_to_dict(plan),
                actions=[],
                events=[],
                blocked_by_gating=True,
                fills=[],
                pnl_snapshot=pnl_snap.to_dict(),
                features=features_dict,
            )

        # Step 4: Execution
        state = self._get_state(symbol)
        result = self._engine.evaluate(plan, symbol, state, ts)

        # Record order placement for gating
        place_actions = [a for a in result.actions if a.action_type.value == "PLACE"]
        for action in place_actions:
            self._rate_limiter.record_order(ts)
            # PLACE actions always have price and quantity set
            if action.price is not None and action.quantity is not None:
                notional = action.price * action.quantity
                self._risk_gate.record_order(symbol, notional)
            self._orders_placed += 1

        # Update state
        self._states[symbol] = result.state

        # Step 5: Simulate fills for PLACE actions
        # v1.1 (crossing): orders fill only if mid_price crosses/touches the limit price
        # v1.0 (instant): all PLACE orders fill immediately (for backward compat)
        action_dicts = [a.to_dict() for a in result.actions]
        fills = simulate_fills(
            ts, symbol, action_dicts, mid_price=snapshot.mid_price, fill_mode=self._fill_mode
        )

        # Step 6: Apply fills to ledger and get updated PnL
        self._ledger.apply_fills(fills)
        self._total_fills += len(fills)

        # Get updated PnL snapshot after fills
        pnl_snap = self._ledger.get_pnl_snapshot(ts, symbol, snapshot.mid_price)

        # Step 6.5: Process fills through CycleEngine (ADR-017)
        # Generate TP + replenishment intents for each fill
        cycle_intents_list: list[dict[str, Any]] = []
        if self._cycle_enabled and self._cycle_engine is not None and fills:
            # Determine adds_allowed based on controller/gating state
            # In v1: adds_allowed=False if controller is paused or kill-switch latched
            adds_allowed = True
            if (
                self._controller_enabled
                and controller_decision is not None
                and controller_decision.mode == ControllerMode.PAUSE
            ):
                adds_allowed = False
            if self._kill_switch.is_triggered:
                adds_allowed = False

            cycle_result = self._cycle_engine.process_fills(fills, adds_allowed=adds_allowed)
            cycle_intents_list = [i.to_dict() for i in cycle_result.intents]

        # Step 7: Drawdown check (ADR-013)
        # Compute total equity and check drawdown guard
        drawdown_check_dict: dict[str, Any] | None = None
        kill_switch_triggered_now = False
        if self._kill_switch_enabled and self._drawdown_guard is not None:
            # Compute total equity: initial_capital + realized + unrealized
            total_realized = self._ledger.get_total_realized_pnl()
            total_unrealized = Decimal("0")
            for sym, last_price in self._last_prices.items():
                total_unrealized += self._ledger.get_unrealized_pnl(sym, last_price)
            equity = self._initial_capital + total_realized + total_unrealized

            # Update drawdown guard
            drawdown_result = self._drawdown_guard.update(equity)
            drawdown_check_dict = drawdown_result.to_dict()

            # Trip kill-switch if drawdown exceeded threshold
            if drawdown_result.triggered and not self._kill_switch.is_triggered:
                self._kill_switch.trip(
                    KillSwitchReason.DRAWDOWN_LIMIT,
                    ts,
                    {
                        "equity": str(equity),
                        "high_water_mark": str(drawdown_result.high_water_mark),
                        "drawdown_pct": drawdown_result.drawdown_pct,
                        "threshold_pct": drawdown_result.threshold_pct,
                    },
                )
                kill_switch_triggered_now = True

        return PaperOutput(
            ts=ts,
            symbol=symbol,
            prefilter_result=filter_result.to_dict(),
            gating_result=gating_result.to_dict(),
            plan=self._plan_to_dict(plan),
            actions=action_dicts,
            events=[e.to_dict() for e in result.events],
            blocked_by_gating=False,
            fills=[f.to_dict() for f in fills],
            pnl_snapshot=pnl_snap.to_dict(),
            drawdown_check=drawdown_check_dict,
            kill_switch_triggered=kill_switch_triggered_now,
            cycle_intents=cycle_intents_list,
            features=features_dict,
        )

    def run(self, fixture_path: Path) -> PaperResult:
        """Run paper trading loop on fixture.

        Pipeline:
        1. Load all events from fixture
        2. First pass: scan events to populate TopKSelector with prices
        3. Select Top-K symbols by volatility score
        4. Filter events to only include selected symbols
        5. Process filtered events through prefilter -> gating -> policy -> execution

        Args:
            fixture_path: Path to fixture directory

        Returns:
            PaperResult with all outputs and digest
        """
        result = PaperResult(fixture_path=str(fixture_path))

        # Load events
        events = self._load_fixture(fixture_path)
        result.events_processed = len(events)

        if not events:
            result.errors.append("No events found in fixture")
            result.digest = self._compute_digest([])
            return result

        # First pass: populate TopKSelector with prices for scoring
        self._topk_selector.reset()
        for event in events:
            snapshot = self._parse_snapshot(event)
            if snapshot:
                self._topk_selector.record_price(snapshot.ts, snapshot.symbol, snapshot.mid_price)

        # Select Top-K symbols
        topk_result = self._topk_selector.select()
        selected_symbols = set(topk_result.selected)

        # Store Top-K results in output
        result.topk_selected_symbols = topk_result.selected
        result.topk_k = topk_result.k
        result.topk_scores = [s.to_dict() for s in topk_result.scores]

        # Filter events to only include selected symbols
        filtered_events = [
            e for e in events if e.get("symbol") in selected_symbols or e.get("type") != "SNAPSHOT"
        ]

        # Process filtered events in order
        outputs: list[PaperOutput] = []
        for event in filtered_events:
            try:
                snapshot = self._parse_snapshot(event)
                if snapshot:
                    output = self.process_snapshot(snapshot)
                    outputs.append(output)
                    if output.blocked_by_gating:
                        result.events_gated += 1
            except Exception as e:
                result.errors.append(f"Error processing event at ts={event.get('ts')}: {e}")

        result.outputs = outputs
        result.orders_placed = self._orders_placed
        result.orders_blocked = self._orders_blocked
        result.total_fills = self._total_fills

        # Compute final positions and PnL
        final_positions = {}
        total_unrealized = Decimal("0")
        for symbol, pos_state in self._ledger.get_all_positions().items():
            last_price = self._last_prices.get(symbol, Decimal("0"))
            unrealized = self._ledger.get_unrealized_pnl(symbol, last_price)
            total_unrealized += unrealized
            final_positions[symbol] = {
                "quantity": str(pos_state.quantity),
                "avg_entry_price": str(pos_state.avg_entry_price),
                "realized_pnl": str(pos_state.realized_pnl),
                "unrealized_pnl": str(unrealized),
            }

        result.final_positions = final_positions
        result.total_realized_pnl = str(self._ledger.get_total_realized_pnl())
        result.total_unrealized_pnl = str(total_unrealized)

        # Controller results (ADR-011)
        result.controller_enabled = self._controller_enabled
        # FeatureEngine results (ADR-019)
        result.feature_engine_enabled = self._feature_engine_enabled
        if self._controller_enabled:
            # Compute final controller decisions for each symbol
            controller_decisions = []
            for symbol in self._controller.get_all_symbols():
                decision = self._controller.decide(symbol)
                controller_decisions.append(
                    {
                        "symbol": symbol,
                        **decision.to_dict(),
                    }
                )
            result.controller_decisions = controller_decisions

        # Kill-switch results (ADR-013)
        result.kill_switch_enabled = self._kill_switch_enabled
        result.kill_switch_triggered = self._kill_switch.is_triggered
        if self._kill_switch.is_triggered:
            result.kill_switch_state = self._kill_switch.state.to_dict()

        # Compute final equity and drawdown
        if self._kill_switch_enabled and self._drawdown_guard is not None:
            total_realized = self._ledger.get_total_realized_pnl()
            final_equity = self._initial_capital + total_realized + total_unrealized
            result.final_equity = str(final_equity)
            result.high_water_mark = str(self._drawdown_guard.high_water_mark)
            # Compute final drawdown
            if self._drawdown_guard.high_water_mark > 0:
                final_drawdown = float(
                    (self._drawdown_guard.high_water_mark - final_equity)
                    / self._drawdown_guard.high_water_mark
                    * 100
                )
                result.final_drawdown_pct = max(0.0, final_drawdown)

        result.digest = self._compute_digest([o.to_digest_dict() for o in outputs])
        return result

    def _load_fixture(self, fixture_path: Path) -> list[dict[str, Any]]:
        """Load fixture events from directory."""
        events: list[dict[str, Any]] = []

        jsonl_path = fixture_path / "events.jsonl"
        json_path = fixture_path / "events.json"

        if jsonl_path.exists():
            with jsonl_path.open() as f:
                for line in f:
                    if line.strip():
                        events.append(json.loads(line))
        elif json_path.exists():
            with json_path.open() as f:
                events = json.load(f)

        # Sort by timestamp for determinism
        events.sort(key=lambda e: e.get("ts", 0))
        return events

    def _parse_snapshot(self, event: dict[str, Any]) -> Snapshot | None:
        """Parse event dict into Snapshot if it's a SNAPSHOT type."""
        if event.get("type") != "SNAPSHOT":
            return None

        return Snapshot(
            ts=event["ts"],
            symbol=event["symbol"],
            bid_price=Decimal(event["bid_price"]),
            ask_price=Decimal(event["ask_price"]),
            bid_qty=Decimal(event["bid_qty"]),
            ask_qty=Decimal(event["ask_qty"]),
            last_price=Decimal(event["last_price"]),
            last_qty=Decimal(event["last_qty"]),
        )

    def _compute_digest(self, outputs: list[dict[str, Any]]) -> str:
        """Compute deterministic digest of outputs."""
        content = json.dumps(outputs, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def reset(self) -> None:
        """Reset all engine state for fresh run."""
        self._port.reset()
        self._rate_limiter.reset()
        self._risk_gate.reset()
        self._toxicity_gate.reset()
        self._topk_selector.reset()
        self._controller.reset()
        self._ledger.reset()
        self._kill_switch.reset()
        if self._drawdown_guard is not None:
            self._drawdown_guard.reset()
        if self._feature_engine is not None:
            self._feature_engine.reset()
        self._states.clear()
        self._last_prices.clear()
        self._orders_placed = 0
        self._orders_blocked = 0
        self._total_fills = 0
