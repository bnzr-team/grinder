"""Drawdown budget allocator for portfolio-level risk distribution.

This module distributes a portfolio-level drawdown budget across multiple
symbols based on their risk tiers and weights.

Key invariants (ADR-032):
    1. Non-negativity: all budgets >= 0
    2. Conservation: sum(budgets) + residual == portfolio_budget
    3. Determinism: same inputs -> same outputs
    4. Monotonicity: larger portfolio budget -> no symbol budget decreases
    5. Tier ordering: HIGH risk gets <= MED <= LOW budget (at equal weights)

Usage:
    allocator = DdAllocator(config)
    result = allocator.allocate(
        equity=Decimal("100000"),
        portfolio_dd_budget=Decimal("0.20"),  # 20% max drawdown
        candidates=[
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
        ],
    )
    print(result.allocations)  # {"BTCUSDT": Decimal("..."), ...}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class AllocationError(Exception):
    """Non-retryable error during budget allocation.

    Raised when inputs are invalid or constraints cannot be satisfied.
    """

    pass


class RiskTier(Enum):
    """Risk tier for symbol classification.

    Higher risk tiers receive smaller budget allocations (inverse relationship).
    """

    LOW = "low"
    MED = "med"
    HIGH = "high"


# Default risk factors per tier (ADR-032)
# Higher factor = higher risk = smaller budget allocation
DEFAULT_TIER_FACTORS: dict[RiskTier, Decimal] = {
    RiskTier.LOW: Decimal("1.0"),
    RiskTier.MED: Decimal("1.5"),
    RiskTier.HIGH: Decimal("2.0"),
}


@dataclass(frozen=True)
class SymbolCandidate:
    """Input candidate for budget allocation.

    Attributes:
        symbol: Trading pair symbol (e.g., "BTCUSDT")
        tier: Risk tier classification
        weight: Priority weight (default 1.0, higher = more budget)
        enabled: Whether symbol is active for trading
    """

    symbol: str
    tier: RiskTier = RiskTier.MED
    weight: Decimal = field(default_factory=lambda: Decimal("1.0"))
    enabled: bool = True

    def __post_init__(self) -> None:
        """Validate candidate parameters."""
        if not self.symbol:
            raise AllocationError("symbol cannot be empty")
        if self.weight < 0:
            raise AllocationError(f"weight must be >= 0, got {self.weight}")


@dataclass(frozen=True)
class AllocationResult:
    """Output of budget allocation.

    Attributes:
        allocations: Map of symbol -> allocated dd_budget (as fraction 0..1)
        allocations_usd: Map of symbol -> allocated budget in USD
        residual_usd: Unallocated budget due to rounding (goes to cash reserve)
        total_allocated_usd: Sum of all allocated budgets
        portfolio_budget_usd: Original portfolio budget
        enabled_count: Number of enabled symbols
    """

    allocations: dict[str, Decimal]  # symbol -> dd_budget fraction
    allocations_usd: dict[str, Decimal]  # symbol -> budget in USD
    residual_usd: Decimal
    total_allocated_usd: Decimal
    portfolio_budget_usd: Decimal
    enabled_count: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict for logging/audit."""
        return {
            "allocations": {k: str(v) for k, v in self.allocations.items()},
            "allocations_usd": {k: str(v) for k, v in self.allocations_usd.items()},
            "residual_usd": str(self.residual_usd),
            "total_allocated_usd": str(self.total_allocated_usd),
            "portfolio_budget_usd": str(self.portfolio_budget_usd),
            "enabled_count": self.enabled_count,
        }


@dataclass
class DdAllocatorConfig:
    """Configuration for DdAllocator.

    Attributes:
        tier_factors: Risk factors per tier (higher = less budget)
        budget_precision: Decimal places for budget rounding
        min_budget_usd: Minimum budget per symbol (below = 0)
    """

    tier_factors: dict[RiskTier, Decimal] = field(default_factory=DEFAULT_TIER_FACTORS.copy)
    budget_precision: int = 2  # USD cents
    min_budget_usd: Decimal = field(default_factory=lambda: Decimal("1.0"))

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.budget_precision < 0:
            raise AllocationError(f"budget_precision must be >= 0, got {self.budget_precision}")
        if self.min_budget_usd < 0:
            raise AllocationError(f"min_budget_usd must be >= 0, got {self.min_budget_usd}")
        for tier, factor in self.tier_factors.items():
            if factor <= 0:
                raise AllocationError(f"tier_factor for {tier.value} must be > 0, got {factor}")


class DdAllocator:
    """Drawdown budget allocator for portfolio risk distribution.

    Distributes portfolio-level DD budget across symbols based on:
    - Risk tier (HIGH/MED/LOW) - higher risk gets smaller budget
    - Weight (priority multiplier)
    - Enabled status (disabled symbols get 0)

    Algorithm (ADR-032):
        1. Filter to enabled symbols only
        2. Compute risk_weight = user_weight / tier_factor for each symbol
        3. Normalize weights to sum to 1.0
        4. Multiply normalized weights by portfolio_budget_usd
        5. ROUND_DOWN to budget_precision
        6. Residual goes to cash_reserve (not reallocated)

    Thread safety: Stateless, safe to use concurrently.
    """

    def __init__(self, config: DdAllocatorConfig | None = None) -> None:
        """Initialize allocator.

        Args:
            config: Configuration (uses defaults if None)
        """
        self._config = config or DdAllocatorConfig()

    @property
    def config(self) -> DdAllocatorConfig:
        """Get current configuration."""
        return self._config

    def allocate(
        self,
        *,
        equity: Decimal,
        portfolio_dd_budget: Decimal,
        candidates: list[SymbolCandidate],
    ) -> AllocationResult:
        """Allocate portfolio DD budget across symbols.

        This is a pure function: same inputs always produce same outputs.

        Args:
            equity: Current account equity (USD)
            portfolio_dd_budget: Maximum portfolio drawdown as fraction (0.20 = 20%)
            candidates: List of symbol candidates with tiers and weights

        Returns:
            AllocationResult with per-symbol budgets and residual

        Raises:
            AllocationError: If inputs are invalid
        """
        # Validate inputs
        self._validate_inputs(equity, portfolio_dd_budget, candidates)

        # Calculate portfolio budget in USD
        portfolio_budget_usd = equity * portfolio_dd_budget

        # Filter to enabled symbols only
        enabled = [c for c in candidates if c.enabled]

        # Handle edge case: no enabled symbols
        if not enabled:
            return AllocationResult(
                allocations={},
                allocations_usd={},
                residual_usd=portfolio_budget_usd,
                total_allocated_usd=Decimal("0"),
                portfolio_budget_usd=portfolio_budget_usd,
                enabled_count=0,
            )

        # Sort by symbol for deterministic order
        enabled = sorted(enabled, key=lambda c: c.symbol)

        # Compute raw risk weights: user_weight / tier_factor
        raw_weights: list[tuple[str, Decimal]] = []
        for candidate in enabled:
            tier_factor = self._config.tier_factors.get(
                candidate.tier, DEFAULT_TIER_FACTORS[RiskTier.MED]
            )
            risk_weight = candidate.weight / tier_factor
            raw_weights.append((candidate.symbol, risk_weight))

        # Normalize weights to sum to 1.0
        total_weight = sum(w for _, w in raw_weights)
        if total_weight <= 0:
            raise AllocationError("Total weight must be > 0")

        normalized_weights = [(sym, w / total_weight) for sym, w in raw_weights]

        # Compute raw allocations in USD
        raw_allocations_usd = [(sym, portfolio_budget_usd * w) for sym, w in normalized_weights]

        # Round DOWN to preserve conservation invariant
        quantize_exp = Decimal(10) ** -self._config.budget_precision
        rounded_allocations_usd: dict[str, Decimal] = {}

        for symbol, raw_usd in raw_allocations_usd:
            rounded = raw_usd.quantize(quantize_exp, rounding=ROUND_DOWN)
            # Apply minimum threshold
            if rounded < self._config.min_budget_usd:
                rounded = Decimal("0")
            rounded_allocations_usd[symbol] = rounded

        # Calculate totals and residual
        total_allocated_usd = sum(rounded_allocations_usd.values(), Decimal("0"))
        residual_usd = portfolio_budget_usd - total_allocated_usd

        # Convert USD allocations to fractions (dd_budget per symbol)
        allocations: dict[str, Decimal] = {}
        for symbol, usd in rounded_allocations_usd.items():
            if equity > 0:
                allocations[symbol] = usd / equity
            else:
                allocations[symbol] = Decimal("0")

        logger.debug(
            "DdAllocator: %d enabled symbols, total_allocated=%.2f, residual=%.2f",
            len(enabled),
            total_allocated_usd,
            residual_usd,
        )

        return AllocationResult(
            allocations=allocations,
            allocations_usd=rounded_allocations_usd,
            residual_usd=residual_usd,
            total_allocated_usd=total_allocated_usd,
            portfolio_budget_usd=portfolio_budget_usd,
            enabled_count=len(enabled),
        )

    def _validate_inputs(
        self,
        equity: Decimal,
        portfolio_dd_budget: Decimal,
        candidates: list[SymbolCandidate],
    ) -> None:
        """Validate input parameters.

        Raises:
            AllocationError: If any input is invalid
        """
        if equity <= 0:
            raise AllocationError(f"equity must be > 0, got {equity}")
        if portfolio_dd_budget <= 0:
            raise AllocationError(f"portfolio_dd_budget must be > 0, got {portfolio_dd_budget}")
        if portfolio_dd_budget > 1:
            raise AllocationError(f"portfolio_dd_budget must be <= 1.0, got {portfolio_dd_budget}")
        if not candidates:
            raise AllocationError("candidates list cannot be empty")

        # Check for duplicate symbols
        symbols = [c.symbol for c in candidates]
        if len(symbols) != len(set(symbols)):
            raise AllocationError("duplicate symbols in candidates list")
