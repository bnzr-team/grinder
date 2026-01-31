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
from grinder.execution import ExecutionEngine, ExecutionState, NoOpExchangePort
from grinder.gating import GatingResult, RateLimiter, RiskGate, ToxicityGate
from grinder.paper.fills import simulate_fills
from grinder.paper.ledger import Ledger
from grinder.policies.base import GridPlan  # noqa: TC001 - used at runtime
from grinder.policies.grid.static import StaticGridPolicy
from grinder.prefilter import TopKSelector, hard_filter

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

        # Step 1: Prefilter
        features = {
            "spread_bps": snapshot.spread_bps,
            "vol_24h_usd": 100_000_000.0,  # Assume sufficient for paper
            "vol_1h_usd": 10_000_000.0,
        }
        filter_result = hard_filter(symbol, features)

        # Get PnL snapshot even if blocked (mark-to-market)
        pnl_snap = self._ledger.get_pnl_snapshot(ts, symbol, snapshot.mid_price)

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
            )

        # Step 2: Policy evaluation (to estimate notional for gating)
        policy_features = {
            "mid_price": snapshot.mid_price,
        }
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
        action_dicts = [a.to_dict() for a in result.actions]
        fills = simulate_fills(ts, symbol, action_dicts)

        # Step 6: Apply fills to ledger and get updated PnL
        self._ledger.apply_fills(fills)
        self._total_fills += len(fills)

        # Get updated PnL snapshot after fills
        pnl_snap = self._ledger.get_pnl_snapshot(ts, symbol, snapshot.mid_price)

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
        result.digest = self._compute_digest([o.to_dict() for o in outputs])
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
        self._ledger.reset()
        self._states.clear()
        self._last_prices.clear()
        self._orders_placed = 0
        self._orders_blocked = 0
        self._total_fills = 0
