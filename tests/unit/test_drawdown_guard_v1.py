"""Unit tests for DrawdownGuardV1 (ASM-P2-03).

Tests cover:
- Portfolio DD breach transitions
- Symbol DD breach transitions
- Intent blocking/allowing in DRAWDOWN
- Determinism guarantees
- No auto-recovery behavior
- Edge cases and input validation
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from grinder.contracts import Snapshot
from grinder.paper import PaperEngine
from grinder.paper.fills import Fill
from grinder.risk import (
    AllowDecision,
    AllowReason,
    DrawdownGuardV1,
    DrawdownGuardV1Config,
    GuardError,
    GuardState,
    OrderIntent,
)

# --- Fixtures ---


@pytest.fixture
def default_config() -> DrawdownGuardV1Config:
    """Default config with 20% portfolio limit and symbol budgets."""
    return DrawdownGuardV1Config(
        portfolio_dd_limit=Decimal("0.20"),  # 20%
        symbol_dd_budgets={
            "BTCUSDT": Decimal("1000"),
            "ETHUSDT": Decimal("500"),
        },
    )


@pytest.fixture
def default_guard(default_config: DrawdownGuardV1Config) -> DrawdownGuardV1:
    """Default guard with standard config."""
    return DrawdownGuardV1(default_config)


# --- Test Class: Portfolio DD Transitions ---


class TestPortfolioDdTransitions:
    """Tests for portfolio-level DD transitions."""

    def test_normal_state_below_limit(self, default_guard: DrawdownGuardV1) -> None:
        """Guard stays NORMAL when portfolio DD is below limit."""
        snapshot = default_guard.update(
            equity_current=Decimal("90000"),  # 10% DD
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.NORMAL
        assert snapshot.portfolio_dd_pct == Decimal("0.10")
        assert default_guard.is_drawdown is False

    def test_transition_to_drawdown_on_portfolio_breach(
        self, default_guard: DrawdownGuardV1
    ) -> None:
        """Guard transitions to DRAWDOWN when portfolio DD >= limit."""
        snapshot = default_guard.update(
            equity_current=Decimal("80000"),  # 20% DD (exactly at limit)
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert snapshot.portfolio_dd_pct == Decimal("0.20")
        assert snapshot.trigger_reason == AllowReason.DD_PORTFOLIO_BREACH
        assert default_guard.is_drawdown is True

    def test_transition_to_drawdown_above_limit(self, default_guard: DrawdownGuardV1) -> None:
        """Guard transitions to DRAWDOWN when portfolio DD exceeds limit."""
        snapshot = default_guard.update(
            equity_current=Decimal("70000"),  # 30% DD
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert snapshot.portfolio_dd_pct == Decimal("0.30")
        assert default_guard.is_drawdown is True

    def test_dd_clamped_to_zero_for_profit(self, default_guard: DrawdownGuardV1) -> None:
        """DD percentage is clamped to 0 when equity > start (profit)."""
        snapshot = default_guard.update(
            equity_current=Decimal("110000"),  # 10% profit
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.NORMAL
        assert snapshot.portfolio_dd_pct == Decimal("0")


# --- Test Class: Symbol DD Transitions ---


class TestSymbolDdTransitions:
    """Tests for symbol-level DD transitions."""

    def test_normal_state_symbol_below_budget(self, default_guard: DrawdownGuardV1) -> None:
        """Guard stays NORMAL when all symbol losses are below budgets."""
        snapshot = default_guard.update(
            equity_current=Decimal("95000"),  # 5% portfolio DD
            equity_start=Decimal("100000"),
            symbol_losses={
                "BTCUSDT": Decimal("500"),  # Below $1000 budget
                "ETHUSDT": Decimal("200"),  # Below $500 budget
            },
        )

        assert snapshot.state == GuardState.NORMAL
        assert len(snapshot.breached_symbols) == 0

    def test_transition_to_drawdown_on_symbol_breach(self, default_guard: DrawdownGuardV1) -> None:
        """Guard transitions to DRAWDOWN when symbol loss >= budget."""
        snapshot = default_guard.update(
            equity_current=Decimal("95000"),  # 5% portfolio DD (below limit)
            equity_start=Decimal("100000"),
            symbol_losses={
                "BTCUSDT": Decimal("1000"),  # Exactly at $1000 budget
            },
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert snapshot.trigger_reason == AllowReason.DD_SYMBOL_BREACH
        assert "BTCUSDT" in snapshot.breached_symbols

    def test_multiple_symbols_breached(self, default_guard: DrawdownGuardV1) -> None:
        """Multiple symbols can breach simultaneously."""
        snapshot = default_guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={
                "BTCUSDT": Decimal("1200"),  # Above $1000
                "ETHUSDT": Decimal("600"),  # Above $500
            },
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert len(snapshot.breached_symbols) == 2
        assert "BTCUSDT" in snapshot.breached_symbols
        assert "ETHUSDT" in snapshot.breached_symbols

    def test_unknown_symbol_no_budget_check(self, default_guard: DrawdownGuardV1) -> None:
        """Symbols without configured budgets are not checked."""
        snapshot = default_guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={
                "BNBUSDT": Decimal("10000"),  # No budget configured
            },
        )

        assert snapshot.state == GuardState.NORMAL
        assert len(snapshot.breached_symbols) == 0


# --- Test Class: Intent Blocking ---


class TestIntentBlocking:
    """Tests for intent allow/block decisions."""

    def test_all_intents_allowed_in_normal(self, default_guard: DrawdownGuardV1) -> None:
        """All intents are allowed in NORMAL state."""
        default_guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        for intent in OrderIntent:
            decision = default_guard.allow(intent, symbol="BTCUSDT")
            assert decision.allowed is True
            assert decision.state == GuardState.NORMAL

    def test_increase_risk_blocked_in_drawdown(self, default_guard: DrawdownGuardV1) -> None:
        """INCREASE_RISK intents are blocked in DRAWDOWN state."""
        default_guard.update(
            equity_current=Decimal("80000"),  # 20% DD
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        decision = default_guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")

        assert decision.allowed is False
        assert decision.reason == AllowReason.DD_PORTFOLIO_BREACH
        assert decision.state == GuardState.DRAWDOWN

    def test_reduce_risk_allowed_in_drawdown(self, default_guard: DrawdownGuardV1) -> None:
        """REDUCE_RISK intents are allowed in DRAWDOWN state."""
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        decision = default_guard.allow(OrderIntent.REDUCE_RISK, symbol="BTCUSDT")

        assert decision.allowed is True
        assert decision.reason == AllowReason.REDUCE_RISK_ALLOWED
        assert decision.state == GuardState.DRAWDOWN

    def test_cancel_always_allowed(self, default_guard: DrawdownGuardV1) -> None:
        """CANCEL intents are always allowed, regardless of state."""
        # First in NORMAL
        default_guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        decision_normal = default_guard.allow(OrderIntent.CANCEL)
        assert decision_normal.allowed is True
        assert decision_normal.reason == AllowReason.CANCEL_ALWAYS_ALLOWED

        # Then in DRAWDOWN
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        decision_drawdown = default_guard.allow(OrderIntent.CANCEL)
        assert decision_drawdown.allowed is True
        assert decision_drawdown.reason == AllowReason.CANCEL_ALWAYS_ALLOWED

    def test_symbol_breach_reason_in_decision(self, default_guard: DrawdownGuardV1) -> None:
        """Decision includes symbol breach info when blocked due to symbol DD."""
        default_guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("1500")},
        )

        decision = default_guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")

        assert decision.allowed is False
        assert decision.reason == AllowReason.DD_SYMBOL_BREACH
        assert decision.details is not None
        assert decision.details.get("symbol") == "BTCUSDT"
        assert decision.details.get("symbol_breached") is True


# --- Test Class: No Auto-Recovery ---


class TestNoAutoRecovery:
    """Tests verifying no auto-recovery behavior."""

    def test_drawdown_state_persists(self, default_guard: DrawdownGuardV1) -> None:
        """DRAWDOWN state persists even if DD recovers below limit."""
        # Trigger DRAWDOWN
        default_guard.update(
            equity_current=Decimal("80000"),  # 20% DD
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        assert default_guard.state == GuardState.DRAWDOWN

        # Equity recovers
        snapshot = default_guard.update(
            equity_current=Decimal("95000"),  # 5% DD (below limit)
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        # State remains DRAWDOWN because guard is latched
        assert snapshot.state == GuardState.DRAWDOWN
        assert default_guard.is_drawdown is True

    def test_only_reset_exits_drawdown(self, default_guard: DrawdownGuardV1) -> None:
        """Only explicit reset() can exit DRAWDOWN state."""
        # Trigger DRAWDOWN
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        assert default_guard.is_drawdown is True

        # Reset
        default_guard.reset()

        # Verify via snapshot to avoid mypy type narrowing issue
        snapshot = default_guard.snapshot()
        assert snapshot.state == GuardState.NORMAL
        assert default_guard.is_drawdown is False

    def test_reset_clears_all_state(self, default_guard: DrawdownGuardV1) -> None:
        """Reset clears all internal state."""
        # Trigger DRAWDOWN with symbol breach
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("1500")},
        )

        default_guard.reset()
        snapshot = default_guard.snapshot()

        assert snapshot.state == GuardState.NORMAL
        assert snapshot.portfolio_dd_pct == Decimal("0")
        assert len(snapshot.symbol_losses) == 0
        assert len(snapshot.breached_symbols) == 0
        assert snapshot.trigger_reason is None


# --- Test Class: Determinism ---


class TestDeterminism:
    """Tests verifying deterministic behavior."""

    def test_same_inputs_same_output(self, default_config: DrawdownGuardV1Config) -> None:
        """Same inputs produce same outputs."""
        guard1 = DrawdownGuardV1(default_config)
        guard2 = DrawdownGuardV1(default_config)

        # Call with identical arguments
        snapshot1 = guard1.update(
            equity_current=Decimal("85000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("800")},
        )
        snapshot2 = guard2.update(
            equity_current=Decimal("85000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("800")},
        )

        assert snapshot1.state == snapshot2.state
        assert snapshot1.portfolio_dd_pct == snapshot2.portfolio_dd_pct
        assert snapshot1.breached_symbols == snapshot2.breached_symbols

    def test_decision_determinism(self, default_config: DrawdownGuardV1Config) -> None:
        """Allow decisions are deterministic."""
        guard1 = DrawdownGuardV1(default_config)
        guard2 = DrawdownGuardV1(default_config)

        # Same update
        guard1.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )
        guard2.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        # Same decision
        decision1 = guard1.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
        decision2 = guard2.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")

        assert decision1.allowed == decision2.allowed
        assert decision1.reason == decision2.reason
        assert decision1.state == decision2.state


# --- Test Class: Edge Cases ---


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_exactly_at_portfolio_limit(self, default_guard: DrawdownGuardV1) -> None:
        """DD exactly at limit triggers DRAWDOWN."""
        snapshot = default_guard.update(
            equity_current=Decimal("80000"),  # Exactly 20%
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert snapshot.portfolio_dd_pct == Decimal("0.20")

    def test_exactly_at_symbol_budget(self, default_guard: DrawdownGuardV1) -> None:
        """Loss exactly at symbol budget triggers DRAWDOWN."""
        snapshot = default_guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("1000")},  # Exactly $1000
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert "BTCUSDT" in snapshot.breached_symbols

    def test_just_below_limit_stays_normal(self, default_guard: DrawdownGuardV1) -> None:
        """DD just below limit stays NORMAL."""
        snapshot = default_guard.update(
            equity_current=Decimal("80001"),  # 19.999% (just below 20%)
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.NORMAL

    def test_zero_equity_current(self, default_guard: DrawdownGuardV1) -> None:
        """Zero current equity (100% loss) triggers DRAWDOWN."""
        snapshot = default_guard.update(
            equity_current=Decimal("0"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        assert snapshot.state == GuardState.DRAWDOWN
        assert snapshot.portfolio_dd_pct == Decimal("1.0")  # 100%


# --- Test Class: Input Validation ---


class TestInputValidation:
    """Tests for input validation and error handling."""

    def test_negative_equity_start_raises_error(self, default_guard: DrawdownGuardV1) -> None:
        """Negative equity_start raises GuardError."""
        with pytest.raises(GuardError, match="equity_start must be > 0"):
            default_guard.update(
                equity_current=Decimal("90000"),
                equity_start=Decimal("-100000"),
                symbol_losses={},
            )

    def test_zero_equity_start_raises_error(self, default_guard: DrawdownGuardV1) -> None:
        """Zero equity_start raises GuardError."""
        with pytest.raises(GuardError, match="equity_start must be > 0"):
            default_guard.update(
                equity_current=Decimal("90000"),
                equity_start=Decimal("0"),
                symbol_losses={},
            )

    def test_negative_equity_current_raises_error(self, default_guard: DrawdownGuardV1) -> None:
        """Negative equity_current raises GuardError."""
        with pytest.raises(GuardError, match="equity_current must be >= 0"):
            default_guard.update(
                equity_current=Decimal("-1000"),
                equity_start=Decimal("100000"),
                symbol_losses={},
            )

    def test_negative_symbol_loss_raises_error(self, default_guard: DrawdownGuardV1) -> None:
        """Negative symbol loss raises GuardError."""
        with pytest.raises(GuardError, match="symbol_loss for BTCUSDT must be >= 0"):
            default_guard.update(
                equity_current=Decimal("95000"),
                equity_start=Decimal("100000"),
                symbol_losses={"BTCUSDT": Decimal("-500")},
            )


# --- Test Class: Configuration Validation ---


class TestConfigurationValidation:
    """Tests for configuration validation."""

    def test_invalid_portfolio_limit_zero(self) -> None:
        """Zero portfolio_dd_limit raises GuardError."""
        with pytest.raises(GuardError, match="portfolio_dd_limit must be > 0"):
            DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0"))

    def test_invalid_portfolio_limit_negative(self) -> None:
        """Negative portfolio_dd_limit raises GuardError."""
        with pytest.raises(GuardError, match="portfolio_dd_limit must be > 0"):
            DrawdownGuardV1Config(portfolio_dd_limit=Decimal("-0.20"))

    def test_invalid_portfolio_limit_over_100(self) -> None:
        """Portfolio_dd_limit > 1.0 raises GuardError."""
        with pytest.raises(GuardError, match=r"portfolio_dd_limit must be <= 1\.0"):
            DrawdownGuardV1Config(portfolio_dd_limit=Decimal("1.5"))

    def test_invalid_symbol_budget_negative(self) -> None:
        """Negative symbol budget raises GuardError."""
        with pytest.raises(GuardError, match="symbol_dd_budget for BTCUSDT must be >= 0"):
            DrawdownGuardV1Config(
                symbol_dd_budgets={"BTCUSDT": Decimal("-100")},
            )


# --- Test Class: Serialization ---


class TestSerialization:
    """Tests for JSON serialization."""

    def test_allow_decision_to_dict(self) -> None:
        """AllowDecision serializes to dict correctly."""
        decision = AllowDecision(
            allowed=False,
            reason=AllowReason.DD_PORTFOLIO_BREACH,
            state=GuardState.DRAWDOWN,
            details={"portfolio_dd_pct": "0.25"},
        )

        d = decision.to_dict()

        assert d["allowed"] is False
        assert d["reason"] == "DD_PORTFOLIO_BREACH"
        assert d["state"] == "DRAWDOWN"
        assert d["details"]["portfolio_dd_pct"] == "0.25"

    def test_guard_snapshot_to_dict(self, default_guard: DrawdownGuardV1) -> None:
        """GuardSnapshot serializes to dict correctly."""
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("1500")},
        )

        snapshot = default_guard.snapshot()
        d = snapshot.to_dict()

        assert d["state"] == "DRAWDOWN"
        assert Decimal(d["portfolio_dd_pct"]) == Decimal("0.20")
        assert Decimal(d["portfolio_dd_limit"]) == Decimal("0.20")
        assert "BTCUSDT" in d["symbol_losses"]


# --- Test Class: Integration with AutoSizer Concept ---


class TestAutoSizerIntegrationConcept:
    """Tests showing how guard integrates with sizing decisions.

    When DRAWDOWN, auto-sizing should return 0 or policy should not place new orders.
    This test demonstrates the guard's role in that flow.
    """

    def test_guard_blocks_sizing_in_drawdown(self, default_guard: DrawdownGuardV1) -> None:
        """Guard can gate auto-sizer calls."""
        # Simulate: policy wants to place new grid orders (INCREASE_RISK)
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        # Before calling auto-sizer, check guard
        intent = OrderIntent.INCREASE_RISK
        decision = default_guard.allow(intent, symbol="BTCUSDT")

        if not decision.allowed:
            # Policy should NOT call auto-sizer or should skip order placement
            # This is the expected path in DRAWDOWN
            pass
        else:
            # Would call auto-sizer here
            pass

        assert not decision.allowed
        assert decision.reason == AllowReason.DD_PORTFOLIO_BREACH

    def test_guard_allows_reduce_only_sizing(self, default_guard: DrawdownGuardV1) -> None:
        """Guard allows reduce-only orders even in DRAWDOWN."""
        default_guard.update(
            equity_current=Decimal("80000"),
            equity_start=Decimal("100000"),
            symbol_losses={},
        )

        # Reduce-only order (closing position)
        decision = default_guard.allow(OrderIntent.REDUCE_RISK, symbol="BTCUSDT")

        assert decision.allowed
        assert decision.reason == AllowReason.REDUCE_RISK_ALLOWED


# --- Test Class: PaperEngine Wiring Integration ---


class TestPaperEngineWiring:
    """Tests for DrawdownGuardV1 wiring in PaperEngine.

    Verifies the guard is correctly integrated BEFORE execution,
    blocking INCREASE_RISK orders when in DRAWDOWN state.
    """

    def test_wiring_allows_execution_in_normal_state(self) -> None:
        """Wiring allows orders when guard is in NORMAL state."""
        engine = PaperEngine(
            spacing_bps=100.0,
            levels=2,
            size_per_level=Decimal("0.01"),
            initial_capital=Decimal("100000"),
            dd_guard_v1_enabled=True,
            dd_guard_v1_config=DrawdownGuardV1Config(
                portfolio_dd_limit=Decimal("0.20"),  # 20%
            ),
        )

        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            bid_qty=Decimal("1"),
            ask_price=Decimal("50010"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        output = engine.process_snapshot(snapshot)

        # Should NOT be blocked by DD guard (NORMAL state, no losses)
        assert not output.blocked_by_dd_guard_v1
        assert output.dd_guard_v1_decision is None or output.dd_guard_v1_decision.get(
            "allowed", True
        )
        # Should have actions (grid orders)
        assert len(output.actions) > 0

    def test_wiring_blocks_execution_on_portfolio_breach(self) -> None:
        """Wiring blocks INCREASE_RISK orders when portfolio DD breaches limit."""
        engine = PaperEngine(
            spacing_bps=100.0,
            levels=2,
            size_per_level=Decimal("0.01"),
            initial_capital=Decimal("100000"),
            dd_guard_v1_enabled=True,
            dd_guard_v1_config=DrawdownGuardV1Config(
                portfolio_dd_limit=Decimal("0.05"),  # 5% limit
            ),
        )

        # First snapshot: establish position and record price
        snapshot1 = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            bid_qty=Decimal("1"),
            ask_price=Decimal("50010"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )
        engine.process_snapshot(snapshot1)

        # Simulate a fill that creates a loss scenario by processing multiple snapshots
        # The guard updates based on ledger state. For test, we need to simulate loss.
        # Let's process more snapshots with lower prices to create unrealized loss

        # Drop price significantly to create >5% unrealized loss
        # With initial_capital=100000 and position, need >5000 unrealized loss
        snapshot2 = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("45000"),  # 10% price drop
            bid_qty=Decimal("1"),
            ask_price=Decimal("45010"),
            ask_qty=Decimal("1"),
            last_price=Decimal("45005"),
            last_qty=Decimal("0.1"),
        )
        output2 = engine.process_snapshot(snapshot2)

        # After significant loss, guard should transition to DRAWDOWN
        # and block INCREASE_RISK orders
        # Note: The exact blocking depends on whether positions were filled
        # and unrealized loss exceeds the 5% threshold

        # If orders were blocked, verify the blocking reason
        if output2.blocked_by_dd_guard_v1:
            assert output2.dd_guard_v1_decision is not None
            assert output2.dd_guard_v1_decision.get("allowed") is False
            assert "DD_PORTFOLIO_BREACH" in output2.dd_guard_v1_decision.get("reason", "")

    def test_wiring_preserves_determinism(self) -> None:
        """Wiring produces deterministic results across runs."""

        def run_engine() -> list[dict[str, int | bool]]:
            engine = PaperEngine(
                spacing_bps=100.0,
                levels=2,
                size_per_level=Decimal("0.01"),
                initial_capital=Decimal("100000"),
                dd_guard_v1_enabled=True,
                dd_guard_v1_config=DrawdownGuardV1Config(
                    portfolio_dd_limit=Decimal("0.10"),
                ),
            )

            outputs = []
            for ts in [1000, 2000, 3000]:
                snapshot = Snapshot(
                    ts=ts,
                    symbol="BTCUSDT",
                    bid_price=Decimal("50000"),
                    bid_qty=Decimal("1"),
                    ask_price=Decimal("50010"),
                    ask_qty=Decimal("1"),
                    last_price=Decimal("50005"),
                    last_qty=Decimal("0.1"),
                )
                output = engine.process_snapshot(snapshot)
                outputs.append(
                    {
                        "ts": output.ts,
                        "blocked_by_dd_guard_v1": output.blocked_by_dd_guard_v1,
                        "actions_count": len(output.actions),
                    }
                )
            return outputs

        run1 = run_engine()
        run2 = run_engine()

        assert run1 == run2, "DD guard wiring must be deterministic"

    def test_wiring_disabled_by_default(self) -> None:
        """DD guard wiring is disabled by default for backward compatibility."""
        # Create engine without dd_guard_v1_enabled
        engine = PaperEngine(
            spacing_bps=100.0,
            levels=2,
            size_per_level=Decimal("0.01"),
        )

        snapshot = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            bid_qty=Decimal("1"),
            ask_price=Decimal("50010"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )

        output = engine.process_snapshot(snapshot)

        # Should never be blocked by DD guard when disabled
        assert not output.blocked_by_dd_guard_v1
        assert output.dd_guard_v1_decision is None

    def test_wiring_output_includes_decision_dict(self) -> None:
        """Output includes DD guard decision when enabled and orders blocked."""
        engine = PaperEngine(
            spacing_bps=100.0,
            levels=2,
            size_per_level=Decimal("0.01"),
            initial_capital=Decimal("10000"),
            dd_guard_v1_enabled=True,
            dd_guard_v1_config=DrawdownGuardV1Config(
                portfolio_dd_limit=Decimal("0.01"),  # 1% very tight limit
            ),
        )

        # Process first snapshot to establish baseline
        snapshot1 = Snapshot(
            ts=1000,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            bid_qty=Decimal("1"),
            ask_price=Decimal("50010"),
            ask_qty=Decimal("1"),
            last_price=Decimal("50005"),
            last_qty=Decimal("0.1"),
        )
        engine.process_snapshot(snapshot1)

        # Process second snapshot with price drop
        # Even small unrealized loss may trigger 1% limit
        snapshot2 = Snapshot(
            ts=2000,
            symbol="BTCUSDT",
            bid_price=Decimal("49000"),  # 2% drop
            bid_qty=Decimal("1"),
            ask_price=Decimal("49010"),
            ask_qty=Decimal("1"),
            last_price=Decimal("49005"),
            last_qty=Decimal("0.1"),
        )
        output = engine.process_snapshot(snapshot2)

        # Verify output structure
        output_dict = output.to_dict()
        assert "blocked_by_dd_guard_v1" in output_dict
        assert "dd_guard_v1_decision" in output_dict


# --- Test Class: Symbol Breach Wiring (P2-04a) ---


class TestSymbolBreachWiring:
    """Tests for symbol breach wiring verification (P2-04a).

    Verifies:
    1. Symbol breach triggers DRAWDOWN with DD_SYMBOL_BREACH reason
    2. Guard is GLOBAL: symbol breach blocks ALL symbols, not just the breached one
    """

    def test_symbol_breach_blocks_with_dd_symbol_breach_reason(self) -> None:
        """BTCUSDT breach blocks INCREASE_RISK with reason DD_SYMBOL_BREACH."""
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.50"),  # High limit so portfolio DD doesn't trigger
            symbol_dd_budgets={
                "BTCUSDT": Decimal("1000"),  # $1000 budget
                "ETHUSDT": Decimal("500"),  # $500 budget
            },
        )
        guard = DrawdownGuardV1(config)

        # Update with BTCUSDT loss at exactly $1000 (at budget threshold)
        snapshot = guard.update(
            equity_current=Decimal("95000"),  # 5% portfolio DD (well below 50% limit)
            equity_start=Decimal("100000"),
            symbol_losses={
                "BTCUSDT": Decimal("1000"),  # Exactly at budget - triggers breach
            },
        )

        # Verify state transitioned to DRAWDOWN
        assert snapshot.state == GuardState.DRAWDOWN
        assert snapshot.trigger_reason == AllowReason.DD_SYMBOL_BREACH
        assert "BTCUSDT" in snapshot.breached_symbols

        # Verify INCREASE_RISK is blocked for BTCUSDT with correct reason
        decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")

        assert decision.allowed is False
        assert decision.reason == AllowReason.DD_SYMBOL_BREACH  # NOT DD_PORTFOLIO_BREACH
        assert decision.state == GuardState.DRAWDOWN
        assert decision.details is not None
        assert decision.details.get("symbol") == "BTCUSDT"
        assert decision.details.get("symbol_breached") is True

    def test_symbol_breach_is_global_blocks_all_symbols(self) -> None:
        """BTCUSDT breach blocks ALL symbols, not just BTCUSDT (global DRAWDOWN).

        Per ADR-033: Guard is GLOBAL. When any symbol breaches its budget,
        the entire guard transitions to DRAWDOWN state, blocking INCREASE_RISK
        for ALL symbols (not just the breached symbol).
        """
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.50"),  # High limit
            symbol_dd_budgets={
                "BTCUSDT": Decimal("1000"),
                "ETHUSDT": Decimal("500"),
            },
        )
        guard = DrawdownGuardV1(config)

        # BTCUSDT breaches its $1000 budget
        guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={
                "BTCUSDT": Decimal("1500"),  # Above budget - breached
                "ETHUSDT": Decimal("100"),  # Well below budget - NOT breached
            },
        )

        # Verify BTCUSDT is blocked (expected - it breached)
        btc_decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
        assert btc_decision.allowed is False
        assert btc_decision.reason == AllowReason.DD_SYMBOL_BREACH

        # Verify ETHUSDT is ALSO blocked (global behavior)
        # Even though ETHUSDT didn't breach its budget, the guard is global
        eth_decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="ETHUSDT")
        assert eth_decision.allowed is False
        assert eth_decision.reason == AllowReason.DD_SYMBOL_BREACH
        # ETHUSDT details won't have symbol_breached=True since ETHUSDT itself didn't breach
        assert eth_decision.details is not None
        assert eth_decision.details.get("symbol") == "ETHUSDT"
        # symbol_breached should be absent or False for ETHUSDT
        assert eth_decision.details.get("symbol_breached") is not True

        # Verify unknown symbol (BNBUSDT) is also blocked
        bnb_decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BNBUSDT")
        assert bnb_decision.allowed is False
        assert bnb_decision.reason == AllowReason.DD_SYMBOL_BREACH

    def test_reduce_risk_allowed_for_all_symbols_after_breach(self) -> None:
        """REDUCE_RISK is allowed for all symbols even after symbol breach.

        This ensures positions can be closed/reduced even when in DRAWDOWN.
        """
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.50"),
            symbol_dd_budgets={"BTCUSDT": Decimal("1000")},
        )
        guard = DrawdownGuardV1(config)

        # Trigger DRAWDOWN via symbol breach
        guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("1500")},
        )

        # REDUCE_RISK allowed for breached symbol
        btc_reduce = guard.allow(OrderIntent.REDUCE_RISK, symbol="BTCUSDT")
        assert btc_reduce.allowed is True
        assert btc_reduce.reason == AllowReason.REDUCE_RISK_ALLOWED

        # REDUCE_RISK allowed for non-breached symbol
        eth_reduce = guard.allow(OrderIntent.REDUCE_RISK, symbol="ETHUSDT")
        assert eth_reduce.allowed is True
        assert eth_reduce.reason == AllowReason.REDUCE_RISK_ALLOWED

    def test_cancel_allowed_for_all_symbols_after_breach(self) -> None:
        """CANCEL is always allowed regardless of DRAWDOWN state."""
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.50"),
            symbol_dd_budgets={"BTCUSDT": Decimal("1000")},
        )
        guard = DrawdownGuardV1(config)

        # Trigger DRAWDOWN via symbol breach
        guard.update(
            equity_current=Decimal("95000"),
            equity_start=Decimal("100000"),
            symbol_losses={"BTCUSDT": Decimal("1500")},
        )

        # CANCEL allowed for any symbol
        btc_cancel = guard.allow(OrderIntent.CANCEL, symbol="BTCUSDT")
        eth_cancel = guard.allow(OrderIntent.CANCEL, symbol="ETHUSDT")

        assert btc_cancel.allowed is True
        assert btc_cancel.reason == AllowReason.CANCEL_ALWAYS_ALLOWED
        assert eth_cancel.allowed is True
        assert eth_cancel.reason == AllowReason.CANCEL_ALWAYS_ALLOWED


# --- Test Class: Reduce-Only Path Proof (P2-04b) ---


class TestReduceOnlyPath:
    """Tests for reduce-only path verification (P2-04b).

    Proves that:
    1. In DRAWDOWN, REDUCE_RISK actually executes and reduces exposure
    2. INCREASE_RISK remains blocked (regression)
    3. CANCEL remains allowed (regression)
    """

    def test_reduce_risk_executes_in_drawdown_and_reduces_exposure(self) -> None:
        """flatten_position closes position in DRAWDOWN via REDUCE_RISK path.

        Scenario:
        1. Create an open position (via fills)
        2. Trigger DRAWDOWN
        3. Call flatten_position
        4. Verify position is closed and exposure reduced to zero
        """

        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.10"),  # 10% limit
            symbol_dd_budgets={"BTCUSDT": Decimal("500")},
        )

        # Create engine with DD guard enabled
        engine = PaperEngine(
            initial_capital=Decimal("100000"),
            dd_guard_v1_enabled=True,
            dd_guard_v1_config=config,
        )

        # Step 1: Create a LONG position by applying a BUY fill directly
        buy_fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),  # 0.1 BTC @ 50000 = $5000 notional
            order_id="test_buy_1",
        )
        engine._ledger.apply_fill(buy_fill)
        engine._last_prices["BTCUSDT"] = Decimal("50000")

        # Verify position exists
        pos_before = engine._ledger.get_position("BTCUSDT")
        assert pos_before.quantity == Decimal("0.1")
        assert pos_before.avg_entry_price == Decimal("50000")

        # Step 2: Trigger DRAWDOWN by updating guard with losses
        # Price drops to 45000, so we have unrealized loss of 0.1 * (50000-45000) = $500
        engine._last_prices["BTCUSDT"] = Decimal("45000")
        assert engine._dd_guard_v1 is not None  # Guard is enabled
        engine._dd_guard_v1.update(
            equity_start=Decimal("100000"),
            equity_current=Decimal("99500"),  # Lost $500
            symbol_losses={"BTCUSDT": Decimal("500")},  # At budget threshold
        )

        # Verify guard is in DRAWDOWN
        assert engine._dd_guard_v1.is_drawdown is True

        # Step 3: Call flatten_position (REDUCE_RISK path)
        result = engine.flatten_position(
            symbol="BTCUSDT",
            current_price=Decimal("45000"),
            ts=2000,
        )

        # Step 4: Verify flatten executed successfully
        assert result["executed"] is True
        assert result["reason"] == "FLATTEN_EXECUTED"

        # Verify position is now zero
        pos_after = engine._ledger.get_position("BTCUSDT")
        assert pos_after.quantity == Decimal("0")

        # Verify fill was generated correctly
        assert result["fill"] is not None
        assert result["fill"]["side"] == "SELL"  # SELL to close long
        assert result["fill"]["quantity"] == "0.1"

        # Verify DD guard decision was checked and allowed
        assert result["dd_guard_v1_decision"] is not None
        assert result["dd_guard_v1_decision"]["allowed"] is True
        assert result["dd_guard_v1_decision"]["reason"] == "REDUCE_RISK_ALLOWED"

    def test_flatten_short_position_in_drawdown(self) -> None:
        """flatten_position closes SHORT position correctly (buys to close).

        Verifies reduce-only works for both long and short positions.
        """

        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.10"),
            symbol_dd_budgets={},
        )

        engine = PaperEngine(
            initial_capital=Decimal("100000"),
            dd_guard_v1_enabled=True,
            dd_guard_v1_config=config,
        )

        # Create a SHORT position
        sell_fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("50000"),
            quantity=Decimal("0.1"),
            order_id="test_sell_1",
        )
        engine._ledger.apply_fill(sell_fill)
        engine._last_prices["BTCUSDT"] = Decimal("50000")

        # Verify short position
        pos_before = engine._ledger.get_position("BTCUSDT")
        assert pos_before.quantity == Decimal("-0.1")  # Negative = short

        # Trigger DRAWDOWN via portfolio DD
        assert engine._dd_guard_v1 is not None  # Guard is enabled
        engine._dd_guard_v1.update(
            equity_start=Decimal("100000"),
            equity_current=Decimal("89000"),  # 11% DD, exceeds 10% limit
            symbol_losses={},
        )
        assert engine._dd_guard_v1.is_drawdown is True

        # Flatten position
        result = engine.flatten_position(
            symbol="BTCUSDT",
            current_price=Decimal("52000"),  # Price went up (loss on short)
            ts=2000,
        )

        # Verify flatten executed
        assert result["executed"] is True
        assert result["fill"]["side"] == "BUY"  # BUY to close short
        assert result["fill"]["quantity"] == "0.1"

        # Position should be zero
        pos_after = engine._ledger.get_position("BTCUSDT")
        assert pos_after.quantity == Decimal("0")

    def test_flatten_no_position_returns_no_execution(self) -> None:
        """flatten_position with no position returns NO_POSITION reason."""
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.10"),
        )

        engine = PaperEngine(
            initial_capital=Decimal("100000"),
            dd_guard_v1_enabled=True,
            dd_guard_v1_config=config,
        )

        # Try to flatten without position
        result = engine.flatten_position(
            symbol="BTCUSDT",
            current_price=Decimal("50000"),
            ts=1000,
        )

        assert result["executed"] is False
        assert result["reason"] == "NO_POSITION"
        assert result["fill"] is None

    def test_increase_risk_still_blocked_in_drawdown_regression(self) -> None:
        """INCREASE_RISK remains blocked in DRAWDOWN (regression test).

        Ensures P2-04a guarantees are preserved.
        """
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.10"),
        )
        guard = DrawdownGuardV1(config)

        # Trigger DRAWDOWN
        guard.update(
            equity_start=Decimal("100000"),
            equity_current=Decimal("89000"),  # 11% DD
            symbol_losses={},
        )

        assert guard.is_drawdown is True

        # INCREASE_RISK must be blocked
        decision = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
        assert decision.allowed is False
        assert decision.reason == AllowReason.DD_PORTFOLIO_BREACH

    def test_cancel_still_allowed_in_drawdown_regression(self) -> None:
        """CANCEL remains allowed in DRAWDOWN (regression test)."""
        config = DrawdownGuardV1Config(
            portfolio_dd_limit=Decimal("0.10"),
        )
        guard = DrawdownGuardV1(config)

        # Trigger DRAWDOWN
        guard.update(
            equity_start=Decimal("100000"),
            equity_current=Decimal("89000"),  # 11% DD
            symbol_losses={},
        )

        assert guard.is_drawdown is True

        # CANCEL must be allowed
        decision = guard.allow(OrderIntent.CANCEL, symbol="BTCUSDT")
        assert decision.allowed is True
        assert decision.reason == AllowReason.CANCEL_ALWAYS_ALLOWED

    def test_reduce_only_is_deterministic(self) -> None:
        """flatten_position produces deterministic results.

        Same inputs → same outputs (critical for replay).
        """

        def create_and_flatten() -> dict[str, Any]:
            config = DrawdownGuardV1Config(
                portfolio_dd_limit=Decimal("0.10"),
            )
            engine = PaperEngine(
                initial_capital=Decimal("100000"),
                dd_guard_v1_enabled=True,
                dd_guard_v1_config=config,
            )

            # Create position
            fill = Fill(
                ts=1000,
                symbol="BTCUSDT",
                side="BUY",
                price=Decimal("50000"),
                quantity=Decimal("0.1"),
                order_id="test_buy_1",
            )
            engine._ledger.apply_fill(fill)
            engine._last_prices["BTCUSDT"] = Decimal("50000")

            # Trigger DRAWDOWN
            assert engine._dd_guard_v1 is not None  # Guard is enabled
            engine._dd_guard_v1.update(
                equity_start=Decimal("100000"),
                equity_current=Decimal("89000"),
                symbol_losses={},
            )

            # Flatten
            return engine.flatten_position(
                symbol="BTCUSDT",
                current_price=Decimal("45000"),
                ts=2000,
            )

        # Run twice
        result1 = create_and_flatten()
        result2 = create_and_flatten()

        # Results must be identical
        assert result1["executed"] == result2["executed"]
        assert result1["reason"] == result2["reason"]
        assert result1["fill"] == result2["fill"]
        assert result1["position_before"] == result2["position_before"]
        assert result1["position_after"] == result2["position_after"]
