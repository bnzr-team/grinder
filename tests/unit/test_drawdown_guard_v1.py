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

import pytest

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
