"""Auto-sizer for risk-budget-based position sizing.

This module computes order quantities per grid level such that
worst-case portfolio loss stays within the drawdown budget.

Key formula (ADR-031):
    worst_case_loss = sum(qty_i * price_i) * adverse_move
    constraint: worst_case_loss <= equity * dd_budget

For uniform sizing across N levels:
    qty_per_level = (equity * dd_budget) / (N * avg_price * adverse_move)

The module supports:
- Uniform sizing (same qty at each level)
- Pyramid sizing (larger qty at outer levels)
- Top-K constraint (only size first K levels)
- Tick/lot size rounding with risk bound verification

Usage:
    sizer = AutoSizer(config)
    schedule = sizer.compute(
        equity=Decimal("10000"),
        dd_budget=Decimal("0.20"),      # 20% max drawdown
        adverse_move=Decimal("0.25"),   # 25% worst-case move
        grid_shape=GridShape(levels=5, step_bps=10, top_k=3),
        price=Decimal("50000"),
    )
    print(schedule.qty_per_level)  # [Decimal("0.01"), ...]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SizingError(Exception):
    """Non-retryable error during position sizing.

    Raised when inputs are invalid or constraints cannot be satisfied.
    This is a configuration error, not a transient failure.
    """

    pass


class SizingMode(Enum):
    """Sizing distribution mode across grid levels.

    UNIFORM: Same quantity at each level (simplest, v0 default)
    PYRAMID: Larger quantities at outer levels (more aggressive)
    INVERSE_PYRAMID: Smaller quantities at outer levels (more conservative)
    """

    UNIFORM = "uniform"
    PYRAMID = "pyramid"
    INVERSE_PYRAMID = "inverse_pyramid"


@dataclass(frozen=True)
class GridShape:
    """Grid structure parameters for sizing calculation.

    Attributes:
        levels: Total number of levels per side (buy or sell)
        step_bps: Spacing between levels in basis points
        top_k: Only place orders at first K levels (0 = all levels)
    """

    levels: int
    step_bps: float
    top_k: int = 0  # 0 means use all levels

    def __post_init__(self) -> None:
        """Validate grid shape parameters."""
        if self.levels < 1:
            raise SizingError(f"levels must be >= 1, got {self.levels}")
        if self.step_bps <= 0:
            raise SizingError(f"step_bps must be > 0, got {self.step_bps}")
        if self.top_k < 0:
            raise SizingError(f"top_k must be >= 0, got {self.top_k}")

    @property
    def effective_levels(self) -> int:
        """Number of levels that will actually be sized."""
        if self.top_k == 0:
            return self.levels
        return min(self.top_k, self.levels)


@dataclass(frozen=True)
class SizeSchedule:
    """Output of auto-sizer: quantities per level with risk metrics.

    Attributes:
        qty_per_level: List of quantities for each level (base asset units)
        total_notional: Sum of all level notionals (price * qty)
        worst_case_loss: Estimated loss if all levels fill and price moves adversely
        risk_utilization: worst_case_loss / (equity * dd_budget) - should be <= 1.0
        effective_levels: Number of levels with non-zero quantity
        sizing_mode: Distribution mode used
    """

    qty_per_level: list[Decimal]
    total_notional: Decimal
    worst_case_loss: Decimal
    risk_utilization: Decimal
    effective_levels: int
    sizing_mode: SizingMode

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict for logging/audit."""
        return {
            "qty_per_level": [str(q) for q in self.qty_per_level],
            "total_notional": str(self.total_notional),
            "worst_case_loss": str(self.worst_case_loss),
            "risk_utilization": str(self.risk_utilization),
            "effective_levels": self.effective_levels,
            "sizing_mode": self.sizing_mode.value,
        }


@dataclass
class AutoSizerConfig:
    """Configuration for AutoSizer.

    Attributes:
        sizing_mode: Distribution mode (UNIFORM, PYRAMID, etc.)
        min_qty: Minimum quantity per level (exchange lot size)
        qty_precision: Decimal places for quantity rounding
        max_risk_utilization: Maximum allowed risk_utilization (default 1.0)
        pyramid_exponent: Exponent for PYRAMID/INVERSE_PYRAMID (default 1.5)
    """

    sizing_mode: SizingMode = SizingMode.UNIFORM
    min_qty: Decimal = field(default_factory=lambda: Decimal("0.0001"))
    qty_precision: int = 8
    max_risk_utilization: Decimal = field(default_factory=lambda: Decimal("1.0"))
    pyramid_exponent: Decimal = field(default_factory=lambda: Decimal("1.5"))

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.min_qty < 0:
            raise SizingError(f"min_qty must be >= 0, got {self.min_qty}")
        if self.qty_precision < 0:
            raise SizingError(f"qty_precision must be >= 0, got {self.qty_precision}")
        if self.max_risk_utilization <= 0:
            raise SizingError(f"max_risk_utilization must be > 0, got {self.max_risk_utilization}")


class AutoSizer:
    """Automatic position sizer based on risk budget.

    Computes order quantities such that worst-case portfolio loss
    stays within the specified drawdown budget.

    Thread safety: Stateless, safe to use concurrently.

    Example:
        sizer = AutoSizer()
        schedule = sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=GridShape(levels=5, step_bps=10),
            price=Decimal("50000"),
        )
    """

    def __init__(self, config: AutoSizerConfig | None = None) -> None:
        """Initialize auto-sizer.

        Args:
            config: Configuration (uses defaults if None)
        """
        self._config = config or AutoSizerConfig()

    @property
    def config(self) -> AutoSizerConfig:
        """Get current configuration."""
        return self._config

    def compute(
        self,
        *,
        equity: Decimal,
        dd_budget: Decimal,
        adverse_move: Decimal,
        grid_shape: GridShape,
        price: Decimal,
    ) -> SizeSchedule:
        """Compute size schedule from risk parameters.

        This is a pure function: same inputs always produce same outputs.

        Args:
            equity: Current account equity (USD or quote currency)
            dd_budget: Maximum drawdown as decimal (0.20 = 20%)
            adverse_move: Worst-case price move as decimal (0.25 = 25%)
            grid_shape: Grid structure (levels, step, top_k)
            price: Current market price (for notional calculation)

        Returns:
            SizeSchedule with quantities and risk metrics

        Raises:
            SizingError: If inputs are invalid or constraints cannot be satisfied

        Formula (ADR-031):
            max_loss_usd = equity * dd_budget
            For uniform sizing across N effective levels:
                total_qty = max_loss_usd / (price * adverse_move)
                qty_per_level = total_qty / N

            Worst-case assumes all levels fill on one side and price
            moves by adverse_move in the unfavorable direction.
        """
        # Validate inputs
        self._validate_inputs(equity, dd_budget, adverse_move, price)

        # Calculate risk budget in USD
        max_loss_usd = equity * dd_budget

        # Effective levels (considering top_k)
        n_levels = grid_shape.effective_levels

        # Total position allowed given risk budget (see ADR-031 for formula)
        total_qty_allowed = max_loss_usd / (price * adverse_move)

        # Compute weights based on sizing mode
        weights = self._compute_weights(n_levels)

        # Distribute quantity across levels
        raw_quantities = [total_qty_allowed * w for w in weights]

        # Round to exchange precision (always round DOWN to stay within budget)
        quantize_exp = Decimal(10) ** -self._config.qty_precision
        rounded_quantities = [q.quantize(quantize_exp, rounding=ROUND_DOWN) for q in raw_quantities]

        # Apply minimum quantity threshold
        final_quantities = []
        for q in rounded_quantities:
            if q < self._config.min_qty:
                final_quantities.append(Decimal("0"))
            else:
                final_quantities.append(q)

        # Pad to full grid length (zeros for levels beyond top_k)
        qty_per_level = final_quantities + [Decimal("0")] * (grid_shape.levels - n_levels)

        # Calculate actual metrics
        total_notional = sum((q * price for q in qty_per_level), Decimal("0"))
        worst_case_loss = total_notional * adverse_move
        risk_utilization = worst_case_loss / max_loss_usd if max_loss_usd > 0 else Decimal("0")
        effective_levels = sum(1 for q in qty_per_level if q > 0)

        # Verify risk bound (should be <= 1.0 due to rounding down)
        if risk_utilization > self._config.max_risk_utilization:
            logger.warning(
                "Risk utilization %.4f exceeds max %.4f after rounding",
                risk_utilization,
                self._config.max_risk_utilization,
            )

        logger.debug(
            "AutoSizer computed: %d levels, total_notional=%.2f, "
            "worst_case_loss=%.2f, utilization=%.4f",
            effective_levels,
            total_notional,
            worst_case_loss,
            risk_utilization,
        )

        return SizeSchedule(
            qty_per_level=qty_per_level,
            total_notional=total_notional,
            worst_case_loss=worst_case_loss,
            risk_utilization=risk_utilization,
            effective_levels=effective_levels,
            sizing_mode=self._config.sizing_mode,
        )

    def _validate_inputs(
        self,
        equity: Decimal,
        dd_budget: Decimal,
        adverse_move: Decimal,
        price: Decimal,
    ) -> None:
        """Validate input parameters.

        Raises:
            SizingError: If any input is invalid
        """
        if equity <= 0:
            raise SizingError(f"equity must be > 0, got {equity}")
        if dd_budget <= 0:
            raise SizingError(f"dd_budget must be > 0, got {dd_budget}")
        if dd_budget > 1:
            raise SizingError(f"dd_budget must be <= 1.0, got {dd_budget}")
        if adverse_move <= 0:
            raise SizingError(f"adverse_move must be > 0, got {adverse_move}")
        if adverse_move > 1:
            raise SizingError(f"adverse_move must be <= 1.0, got {adverse_move}")
        if price <= 0:
            raise SizingError(f"price must be > 0, got {price}")

    def _compute_weights(self, n_levels: int) -> list[Decimal]:
        """Compute weight distribution across levels.

        Returns list of weights that sum to 1.0.

        Args:
            n_levels: Number of levels to size

        Returns:
            List of Decimal weights, one per level
        """
        if n_levels == 0:
            return []

        mode = self._config.sizing_mode

        if mode == SizingMode.UNIFORM:
            # Equal weight at each level
            weight = Decimal("1") / Decimal(n_levels)
            return [weight] * n_levels

        elif mode == SizingMode.PYRAMID:
            # Larger weights at outer levels (higher index)
            # weight_i = i^exponent, then normalize
            exp = float(self._config.pyramid_exponent)
            raw_weights = [Decimal(str((i + 1) ** exp)) for i in range(n_levels)]
            total = sum(raw_weights)
            return [w / total for w in raw_weights]

        elif mode == SizingMode.INVERSE_PYRAMID:
            # Smaller weights at outer levels (lower index gets more)
            # weight_i = (N - i)^exponent, then normalize
            exp = float(self._config.pyramid_exponent)
            raw_weights = [Decimal(str((n_levels - i) ** exp)) for i in range(n_levels)]
            total = sum(raw_weights)
            return [w / total for w in raw_weights]

        else:
            # Fallback to uniform
            weight = Decimal("1") / Decimal(n_levels)
            return [weight] * n_levels
