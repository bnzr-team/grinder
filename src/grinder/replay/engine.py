"""Replay engine for end-to-end deterministic backtesting.

This module provides the core replay loop that:
1. Loads fixture events (Snapshots)
2. Applies prefilter gates
3. Evaluates policy to get GridPlan
4. Executes via ExecutionEngine
5. Produces deterministic output digest

See: docs/11_BACKTEST_PROTOCOL.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path  # noqa: TC003 - used at runtime (fixture_path / ...)
from typing import Any

from grinder.contracts import Snapshot
from grinder.execution import ExecutionEngine, ExecutionState, NoOpExchangePort
from grinder.policies.base import GridPlan  # noqa: TC001 - used at runtime (.value)
from grinder.policies.grid.static import StaticGridPolicy
from grinder.prefilter import hard_filter


@dataclass
class ReplayOutput:
    """Single replay cycle output."""

    ts: int
    symbol: str
    prefilter_result: dict[str, Any]
    plan: dict[str, Any] | None
    actions: list[dict[str, Any]]
    events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "prefilter_result": self.prefilter_result,
            "plan": self.plan,
            "actions": self.actions,
            "events": self.events,
        }


@dataclass
class ReplayResult:
    """Complete replay result."""

    fixture_path: str
    outputs: list[ReplayOutput] = field(default_factory=list)
    digest: str = ""
    events_processed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "fixture_path": self.fixture_path,
            "outputs": [o.to_dict() for o in self.outputs],
            "digest": self.digest,
            "events_processed": self.events_processed,
            "errors": self.errors,
        }

    def to_json(self) -> str:
        """Serialize to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


class ReplayEngine:
    """End-to-end replay engine for deterministic backtesting.

    Wires together: prefilter -> policy -> execution
    All state is maintained internally for determinism.
    """

    def __init__(
        self,
        spacing_bps: float = 10.0,
        levels: int = 5,
        size_per_level: Decimal = Decimal("100"),
        price_precision: int = 2,
        quantity_precision: int = 3,
    ) -> None:
        """Initialize replay engine.

        Args:
            spacing_bps: Grid spacing in basis points
            levels: Number of levels on each side
            size_per_level: Order size per level
            price_precision: Decimal places for price
            quantity_precision: Decimal places for quantity
        """
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
        # Per-symbol execution state
        self._states: dict[str, ExecutionState] = {}

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

    def process_snapshot(self, snapshot: Snapshot) -> ReplayOutput:
        """Process single snapshot through full pipeline.

        Args:
            snapshot: Market data snapshot

        Returns:
            ReplayOutput with prefilter, plan, and execution results
        """
        symbol = snapshot.symbol
        ts = snapshot.ts

        # Step 1: Prefilter
        # Build features from snapshot for prefilter
        features = {
            "spread_bps": snapshot.spread_bps,
            "vol_24h_usd": 100_000_000.0,  # Assume sufficient volume for replay
            "vol_1h_usd": 10_000_000.0,
        }
        filter_result = hard_filter(symbol, features)

        # If blocked, return early with no actions
        if not filter_result.allowed:
            return ReplayOutput(
                ts=ts,
                symbol=symbol,
                prefilter_result=filter_result.to_dict(),
                plan=None,
                actions=[],
                events=[],
            )

        # Step 2: Policy evaluation
        policy_features = {
            "mid_price": snapshot.mid_price,
        }
        plan = self._policy.evaluate(policy_features)

        # Step 3: Execution
        state = self._get_state(symbol)
        result = self._engine.evaluate(plan, symbol, state, ts)

        # Update state
        self._states[symbol] = result.state

        return ReplayOutput(
            ts=ts,
            symbol=symbol,
            prefilter_result=filter_result.to_dict(),
            plan=self._plan_to_dict(plan),
            actions=[a.to_dict() for a in result.actions],
            events=[e.to_dict() for e in result.events],
        )

    def run(self, fixture_path: Path) -> ReplayResult:
        """Run full replay on fixture.

        Args:
            fixture_path: Path to fixture directory

        Returns:
            ReplayResult with all outputs and digest
        """
        result = ReplayResult(fixture_path=str(fixture_path))

        # Load events
        events = self._load_fixture(fixture_path)
        result.events_processed = len(events)

        if not events:
            result.errors.append("No events found in fixture")
            result.digest = self._compute_digest([])
            return result

        # Process events in order
        outputs: list[ReplayOutput] = []
        for event in events:
            try:
                snapshot = self._parse_snapshot(event)
                if snapshot:
                    output = self.process_snapshot(snapshot)
                    outputs.append(output)
            except Exception as e:
                result.errors.append(f"Error processing event at ts={event.get('ts')}: {e}")

        result.outputs = outputs
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
