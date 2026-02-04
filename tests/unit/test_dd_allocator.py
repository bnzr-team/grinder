"""Unit tests for DdAllocator.

Tests cover the five invariants from ADR-032:
1. Non-negativity: all budgets >= 0
2. Conservation: sum(budgets) + residual == portfolio_budget
3. Determinism: same inputs -> same outputs
4. Monotonicity: larger portfolio budget -> no symbol budget decreases
5. Tier ordering: HIGH risk gets <= MED <= LOW budget (at equal weights)

Plus edge cases and configuration tests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.sizing import (
    AllocationError,
    DdAllocator,
    DdAllocatorConfig,
    RiskTier,
    SymbolCandidate,
)

# --- Fixtures ---


@pytest.fixture
def default_allocator() -> DdAllocator:
    """Default allocator with standard config."""
    return DdAllocator()


@pytest.fixture
def standard_candidates() -> list[SymbolCandidate]:
    """Standard 3-symbol candidate list with different tiers."""
    return [
        SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
        SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
        SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
    ]


# --- Test Class: Non-Negativity Invariant ---


class TestNonNegativity:
    """Tests for non-negativity invariant: all budgets >= 0."""

    def test_all_allocations_non_negative(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """All allocated budgets should be >= 0."""
        result = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )

        for symbol, budget in result.allocations.items():
            assert budget >= 0, f"{symbol} has negative budget: {budget}"

        for symbol, budget_usd in result.allocations_usd.items():
            assert budget_usd >= 0, f"{symbol} has negative USD budget: {budget_usd}"

        assert result.residual_usd >= 0, "Residual should be non-negative"

    def test_small_equity_non_negative(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Even with small equity, allocations should be non-negative."""
        result = default_allocator.allocate(
            equity=Decimal("100"),
            portfolio_dd_budget=Decimal("0.05"),
            candidates=standard_candidates,
        )

        for budget in result.allocations.values():
            assert budget >= 0

        for budget_usd in result.allocations_usd.values():
            assert budget_usd >= 0


# --- Test Class: Conservation Invariant ---


class TestConservation:
    """Tests for conservation invariant: sum(budgets) + residual == portfolio_budget."""

    def test_budget_conservation_exact(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Sum of allocations + residual should equal portfolio budget exactly."""
        equity = Decimal("100000")
        dd_budget = Decimal("0.20")
        portfolio_budget_usd = equity * dd_budget

        result = default_allocator.allocate(
            equity=equity,
            portfolio_dd_budget=dd_budget,
            candidates=standard_candidates,
        )

        total = result.total_allocated_usd + result.residual_usd
        assert total == portfolio_budget_usd, (
            f"Conservation violated: {result.total_allocated_usd} + "
            f"{result.residual_usd} != {portfolio_budget_usd}"
        )

    def test_conservation_with_many_symbols(self, default_allocator: DdAllocator) -> None:
        """Conservation should hold with many symbols."""
        candidates = [SymbolCandidate(symbol=f"SYM{i}USDT", tier=RiskTier.MED) for i in range(20)]

        equity = Decimal("1000000")
        dd_budget = Decimal("0.15")
        portfolio_budget_usd = equity * dd_budget

        result = default_allocator.allocate(
            equity=equity,
            portfolio_dd_budget=dd_budget,
            candidates=candidates,
        )

        total = result.total_allocated_usd + result.residual_usd
        assert total == portfolio_budget_usd

    def test_conservation_with_disabled_symbols(self, default_allocator: DdAllocator) -> None:
        """Conservation should hold when some symbols are disabled."""
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH, enabled=True),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED, enabled=False),
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW, enabled=True),
        ]

        equity = Decimal("50000")
        dd_budget = Decimal("0.25")
        portfolio_budget_usd = equity * dd_budget

        result = default_allocator.allocate(
            equity=equity,
            portfolio_dd_budget=dd_budget,
            candidates=candidates,
        )

        total = result.total_allocated_usd + result.residual_usd
        assert total == portfolio_budget_usd


# --- Test Class: Determinism Invariant ---


class TestDeterminism:
    """Tests for determinism invariant: same inputs -> same outputs."""

    def test_same_inputs_same_output(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Same inputs should always produce identical outputs."""
        result1 = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )
        result2 = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )

        assert result1.allocations == result2.allocations
        assert result1.allocations_usd == result2.allocations_usd
        assert result1.residual_usd == result2.residual_usd

    def test_deterministic_across_instances(
        self, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Different allocator instances with same config produce same results."""
        config = DdAllocatorConfig()
        allocator1 = DdAllocator(config)
        allocator2 = DdAllocator(config)

        result1 = allocator1.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )
        result2 = allocator2.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )

        assert result1.allocations == result2.allocations
        assert result1.allocations_usd == result2.allocations_usd

    def test_order_independent_determinism(self, default_allocator: DdAllocator) -> None:
        """Different input order should produce same output (sorted internally)."""
        candidates_order1 = [
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
        ]
        candidates_order2 = [
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
        ]

        result1 = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates_order1,
        )
        result2 = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates_order2,
        )

        assert result1.allocations == result2.allocations


# --- Test Class: Monotonicity Invariant ---


class TestMonotonicity:
    """Tests for monotonicity: larger portfolio budget -> no symbol budget decreases."""

    def test_larger_budget_no_decrease(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Increasing portfolio budget should not decrease any symbol's allocation."""
        result_small = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.10"),
            candidates=standard_candidates,
        )
        result_large = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )

        for symbol in result_small.allocations_usd:
            assert result_large.allocations_usd[symbol] >= result_small.allocations_usd[symbol], (
                f"{symbol} budget decreased from {result_small.allocations_usd[symbol]} "
                f"to {result_large.allocations_usd[symbol]}"
            )

    def test_monotonicity_with_equity_increase(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Increasing equity should not decrease any symbol's USD allocation."""
        result_small = default_allocator.allocate(
            equity=Decimal("50000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )
        result_large = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )

        for symbol in result_small.allocations_usd:
            assert result_large.allocations_usd[symbol] >= result_small.allocations_usd[symbol]


# --- Test Class: Tier Ordering Invariant ---


class TestTierOrdering:
    """Tests for tier ordering: HIGH risk gets <= MED <= LOW budget (at equal weights)."""

    def test_tier_ordering_equal_weights(self, default_allocator: DdAllocator) -> None:
        """At equal weights, LOW tier should get most budget, HIGH least."""
        candidates = [
            SymbolCandidate(symbol="HIGH_SYM", tier=RiskTier.HIGH, weight=Decimal("1.0")),
            SymbolCandidate(symbol="MED_SYM", tier=RiskTier.MED, weight=Decimal("1.0")),
            SymbolCandidate(symbol="LOW_SYM", tier=RiskTier.LOW, weight=Decimal("1.0")),
        ]

        result = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.30"),
            candidates=candidates,
        )

        high_budget = result.allocations_usd["HIGH_SYM"]
        med_budget = result.allocations_usd["MED_SYM"]
        low_budget = result.allocations_usd["LOW_SYM"]

        assert high_budget <= med_budget, f"HIGH {high_budget} > MED {med_budget}"
        assert med_budget <= low_budget, f"MED {med_budget} > LOW {low_budget}"

    def test_tier_ordering_same_tier_same_budget(self, default_allocator: DdAllocator) -> None:
        """Symbols with same tier and weight should get same budget."""
        candidates = [
            SymbolCandidate(symbol="SYM_A", tier=RiskTier.MED, weight=Decimal("1.0")),
            SymbolCandidate(symbol="SYM_B", tier=RiskTier.MED, weight=Decimal("1.0")),
            SymbolCandidate(symbol="SYM_C", tier=RiskTier.MED, weight=Decimal("1.0")),
        ]

        result = default_allocator.allocate(
            equity=Decimal("90000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        budgets = list(result.allocations_usd.values())
        assert budgets[0] == budgets[1] == budgets[2], f"Unequal budgets: {budgets}"


# --- Test Class: Disabled Symbols ---


class TestDisabledSymbols:
    """Tests for disabled symbol handling."""

    def test_disabled_symbols_get_zero(self, default_allocator: DdAllocator) -> None:
        """Disabled symbols should receive zero budget."""
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH, enabled=True),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED, enabled=False),
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW, enabled=True),
        ]

        result = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        # Disabled symbol not in allocations
        assert "ETHUSDT" not in result.allocations
        assert "ETHUSDT" not in result.allocations_usd
        assert result.enabled_count == 2

    def test_all_disabled_returns_full_residual(self, default_allocator: DdAllocator) -> None:
        """All disabled symbols should return full budget as residual."""
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH, enabled=False),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED, enabled=False),
        ]

        equity = Decimal("100000")
        dd_budget = Decimal("0.20")
        portfolio_budget_usd = equity * dd_budget

        result = default_allocator.allocate(
            equity=equity,
            portfolio_dd_budget=dd_budget,
            candidates=candidates,
        )

        assert result.allocations == {}
        assert result.allocations_usd == {}
        assert result.residual_usd == portfolio_budget_usd
        assert result.enabled_count == 0


# --- Test Class: Weight Impact ---


class TestWeightImpact:
    """Tests for custom weight handling."""

    def test_higher_weight_more_budget(self, default_allocator: DdAllocator) -> None:
        """Symbol with higher weight should get more budget."""
        candidates = [
            SymbolCandidate(symbol="LOW_WEIGHT", tier=RiskTier.MED, weight=Decimal("1.0")),
            SymbolCandidate(symbol="HIGH_WEIGHT", tier=RiskTier.MED, weight=Decimal("3.0")),
        ]

        result = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        low_budget = result.allocations_usd["LOW_WEIGHT"]
        high_budget = result.allocations_usd["HIGH_WEIGHT"]

        assert high_budget > low_budget, (
            f"Higher weight didn't get more: {high_budget} vs {low_budget}"
        )

    def test_weight_can_override_tier(self, default_allocator: DdAllocator) -> None:
        """Very high weight can make HIGH tier symbol get more than LOW tier."""
        candidates = [
            SymbolCandidate(symbol="HIGH_TIER", tier=RiskTier.HIGH, weight=Decimal("10.0")),
            SymbolCandidate(symbol="LOW_TIER", tier=RiskTier.LOW, weight=Decimal("1.0")),
        ]

        result = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        high_tier_budget = result.allocations_usd["HIGH_TIER"]
        low_tier_budget = result.allocations_usd["LOW_TIER"]

        # With 10x weight, HIGH tier should get more despite higher risk factor
        assert high_tier_budget > low_tier_budget


# --- Test Class: Edge Cases ---


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_single_symbol(self, default_allocator: DdAllocator) -> None:
        """Single symbol should get full budget (minus rounding)."""
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.MED),
        ]

        equity = Decimal("100000")
        dd_budget = Decimal("0.20")
        portfolio_budget_usd = equity * dd_budget

        result = default_allocator.allocate(
            equity=equity,
            portfolio_dd_budget=dd_budget,
            candidates=candidates,
        )

        # Single symbol gets nearly all budget (residual is rounding only)
        assert result.allocations_usd["BTCUSDT"] == portfolio_budget_usd
        assert result.residual_usd == Decimal("0")

    def test_very_small_budget_below_minimum(self, default_allocator: DdAllocator) -> None:
        """Budget below minimum should be zero."""
        candidates = [SymbolCandidate(symbol=f"SYM{i}USDT", tier=RiskTier.MED) for i in range(100)]

        # Very small budget spread across 100 symbols
        result = default_allocator.allocate(
            equity=Decimal("100"),
            portfolio_dd_budget=Decimal("0.01"),  # $1 total
            candidates=candidates,
        )

        # Each symbol would get $0.01, below $1 minimum
        for budget_usd in result.allocations_usd.values():
            assert budget_usd == Decimal("0")

        # Full budget goes to residual
        assert result.residual_usd == Decimal("1.00")


# --- Test Class: Input Validation ---


class TestInputValidation:
    """Tests for input validation errors."""

    def test_zero_equity_raises_error(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Zero equity should raise AllocationError."""
        with pytest.raises(AllocationError, match="equity must be > 0"):
            default_allocator.allocate(
                equity=Decimal("0"),
                portfolio_dd_budget=Decimal("0.20"),
                candidates=standard_candidates,
            )

    def test_negative_equity_raises_error(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Negative equity should raise AllocationError."""
        with pytest.raises(AllocationError, match="equity must be > 0"):
            default_allocator.allocate(
                equity=Decimal("-1000"),
                portfolio_dd_budget=Decimal("0.20"),
                candidates=standard_candidates,
            )

    def test_zero_dd_budget_raises_error(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """Zero dd_budget should raise AllocationError."""
        with pytest.raises(AllocationError, match="portfolio_dd_budget must be > 0"):
            default_allocator.allocate(
                equity=Decimal("100000"),
                portfolio_dd_budget=Decimal("0"),
                candidates=standard_candidates,
            )

    def test_dd_budget_over_100_raises_error(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """DD budget > 100% should raise AllocationError."""
        with pytest.raises(AllocationError, match=r"portfolio_dd_budget must be <= 1\.0"):
            default_allocator.allocate(
                equity=Decimal("100000"),
                portfolio_dd_budget=Decimal("1.5"),
                candidates=standard_candidates,
            )

    def test_empty_candidates_raises_error(self, default_allocator: DdAllocator) -> None:
        """Empty candidates list should raise AllocationError."""
        with pytest.raises(AllocationError, match="candidates list cannot be empty"):
            default_allocator.allocate(
                equity=Decimal("100000"),
                portfolio_dd_budget=Decimal("0.20"),
                candidates=[],
            )

    def test_duplicate_symbols_raises_error(self, default_allocator: DdAllocator) -> None:
        """Duplicate symbols should raise AllocationError."""
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.MED),  # Duplicate
        ]
        with pytest.raises(AllocationError, match="duplicate symbols"):
            default_allocator.allocate(
                equity=Decimal("100000"),
                portfolio_dd_budget=Decimal("0.20"),
                candidates=candidates,
            )

    def test_empty_symbol_raises_error(self) -> None:
        """Empty symbol name should raise AllocationError."""
        with pytest.raises(AllocationError, match="symbol cannot be empty"):
            SymbolCandidate(symbol="", tier=RiskTier.MED)

    def test_negative_weight_raises_error(self) -> None:
        """Negative weight should raise AllocationError."""
        with pytest.raises(AllocationError, match="weight must be >= 0"):
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.MED, weight=Decimal("-1"))


# --- Test Class: Configuration ---


class TestConfiguration:
    """Tests for allocator configuration."""

    def test_custom_tier_factors(self) -> None:
        """Custom tier factors should affect allocation."""
        # Custom: HIGH gets same factor as LOW (should get equal budgets)
        config = DdAllocatorConfig(
            tier_factors={
                RiskTier.LOW: Decimal("1.0"),
                RiskTier.MED: Decimal("1.0"),
                RiskTier.HIGH: Decimal("1.0"),
            }
        )
        allocator = DdAllocator(config)

        candidates = [
            SymbolCandidate(symbol="HIGH_SYM", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="LOW_SYM", tier=RiskTier.LOW),
        ]

        result = allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        # With equal factors, should get equal budgets
        assert result.allocations_usd["HIGH_SYM"] == result.allocations_usd["LOW_SYM"]

    def test_custom_budget_precision(self) -> None:
        """Custom budget precision should round appropriately."""
        config = DdAllocatorConfig(budget_precision=0)  # Round to whole dollars
        allocator = DdAllocator(config)

        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.MED),
        ]

        result = allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        # Budget should be whole dollars
        budget = result.allocations_usd["BTCUSDT"]
        assert budget == budget.quantize(Decimal("1"))

    def test_custom_min_budget(self) -> None:
        """Custom min_budget should filter small allocations."""
        config = DdAllocatorConfig(min_budget_usd=Decimal("1000"))
        allocator = DdAllocator(config)

        candidates = [SymbolCandidate(symbol=f"SYM{i}USDT", tier=RiskTier.MED) for i in range(30)]

        # $2000 budget / 30 symbols = ~$66 each, below $1000 min
        result = allocator.allocate(
            equity=Decimal("10000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        for budget_usd in result.allocations_usd.values():
            assert budget_usd == Decimal("0")


# --- Test Class: Serialization ---


class TestSerialization:
    """Tests for result serialization."""

    def test_to_dict_serializable(
        self, default_allocator: DdAllocator, standard_candidates: list[SymbolCandidate]
    ) -> None:
        """AllocationResult should be JSON-serializable via to_dict."""
        result = default_allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=standard_candidates,
        )

        d = result.to_dict()

        assert "allocations" in d
        assert "allocations_usd" in d
        assert "residual_usd" in d
        assert "total_allocated_usd" in d
        assert "portfolio_budget_usd" in d
        assert "enabled_count" in d

        # All values should be strings (JSON-safe)
        for v in d["allocations"].values():
            assert isinstance(v, str)
        for v in d["allocations_usd"].values():
            assert isinstance(v, str)
        assert isinstance(d["residual_usd"], str)
