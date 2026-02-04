"""Unit tests for AutoSizer.

Tests cover DoD scenarios:
1. Small equity / large adverse_move → qty decreases
2. Increase dd_budget → qty increases monotonically
3. Step/levels change → schedule changes expectedly
4. Top-K constraint affects distribution
5. Rounding doesn't violate risk bound
6. Edge cases: 0/neg inputs → SizingError
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.sizing import (
    AutoSizer,
    AutoSizerConfig,
    GridShape,
    SizingError,
)
from grinder.sizing.auto_sizer import SizingMode

# --- Fixtures ---


@pytest.fixture
def default_sizer() -> AutoSizer:
    """Default auto-sizer with standard config."""
    return AutoSizer()


@pytest.fixture
def standard_grid() -> GridShape:
    """Standard 5-level grid."""
    return GridShape(levels=5, step_bps=10.0)


# --- Test Class: Risk Budget Behavior ---


class TestRiskBudgetBehavior:
    """Tests for risk budget impact on sizing."""

    def test_small_equity_reduces_qty(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Smaller equity should result in smaller quantities."""
        # Large equity
        large_schedule = default_sizer.compute(
            equity=Decimal("100000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Small equity (10x smaller)
        small_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Quantities should scale proportionally
        assert small_schedule.qty_per_level[0] < large_schedule.qty_per_level[0]
        assert small_schedule.total_notional < large_schedule.total_notional

        # Ratio should be approximately 10:1
        ratio = large_schedule.qty_per_level[0] / small_schedule.qty_per_level[0]
        assert Decimal("9") < ratio < Decimal("11")  # Allow for rounding

    def test_large_adverse_move_reduces_qty(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Larger adverse_move should result in smaller quantities."""
        # Small adverse move (10%)
        small_move_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.10"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Large adverse move (50%)
        large_move_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.50"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Larger adverse move = smaller qty (inverse relationship)
        assert large_move_schedule.qty_per_level[0] < small_move_schedule.qty_per_level[0]

        # Ratio should be approximately 5:1 (0.50/0.10)
        ratio = small_move_schedule.qty_per_level[0] / large_move_schedule.qty_per_level[0]
        assert Decimal("4") < ratio < Decimal("6")

    def test_increase_dd_budget_increases_qty_monotonically(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Increasing dd_budget should monotonically increase quantities."""
        budgets = [Decimal("0.05"), Decimal("0.10"), Decimal("0.20"), Decimal("0.50")]
        schedules = []

        for budget in budgets:
            schedule = default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=budget,
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )
            schedules.append(schedule)

        # Each subsequent schedule should have >= quantities
        for i in range(len(schedules) - 1):
            assert schedules[i + 1].qty_per_level[0] >= schedules[i].qty_per_level[0]
            assert schedules[i + 1].total_notional >= schedules[i].total_notional

    def test_higher_price_reduces_qty(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Higher price should result in smaller base quantities (same notional)."""
        # Low price
        low_price_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("10000"),
        )

        # High price (5x higher)
        high_price_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Higher price = smaller qty in base terms
        assert high_price_schedule.qty_per_level[0] < low_price_schedule.qty_per_level[0]

        # But notional should be approximately the same
        notional_ratio = low_price_schedule.total_notional / high_price_schedule.total_notional
        assert Decimal("0.9") < notional_ratio < Decimal("1.1")


# --- Test Class: Grid Shape Impact ---


class TestGridShapeImpact:
    """Tests for grid shape parameters impact on sizing."""

    def test_more_levels_reduces_qty_per_level(self, default_sizer: AutoSizer) -> None:
        """More levels should result in smaller qty per level (same total risk)."""
        # 3 levels
        small_grid = GridShape(levels=3, step_bps=10.0)
        small_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=small_grid,
            price=Decimal("50000"),
        )

        # 10 levels
        large_grid = GridShape(levels=10, step_bps=10.0)
        large_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=large_grid,
            price=Decimal("50000"),
        )

        # More levels = smaller qty per level
        assert large_schedule.qty_per_level[0] < small_schedule.qty_per_level[0]

        # But total notional should be approximately the same
        notional_ratio = small_schedule.total_notional / large_schedule.total_notional
        assert Decimal("0.8") < notional_ratio < Decimal("1.2")

    def test_top_k_limits_sized_levels(self, default_sizer: AutoSizer) -> None:
        """Top-K should limit which levels get non-zero quantities."""
        # Full grid (5 levels, no top_k)
        full_grid = GridShape(levels=5, step_bps=10.0, top_k=0)
        full_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=full_grid,
            price=Decimal("50000"),
        )

        # Top-3 only
        top3_grid = GridShape(levels=5, step_bps=10.0, top_k=3)
        top3_schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=top3_grid,
            price=Decimal("50000"),
        )

        # Full grid has 5 non-zero levels
        assert full_schedule.effective_levels == 5
        assert len([q for q in full_schedule.qty_per_level if q > 0]) == 5

        # Top-3 has only 3 non-zero levels
        assert top3_schedule.effective_levels == 3
        assert len([q for q in top3_schedule.qty_per_level if q > 0]) == 3

        # Levels 4 and 5 should be zero in top-3
        assert top3_schedule.qty_per_level[3] == Decimal("0")
        assert top3_schedule.qty_per_level[4] == Decimal("0")

        # First 3 levels in top-3 should be larger than full grid
        # (same risk spread over fewer levels)
        for i in range(3):
            assert top3_schedule.qty_per_level[i] > full_schedule.qty_per_level[i]

    def test_schedule_length_matches_grid_levels(self, default_sizer: AutoSizer) -> None:
        """Size schedule should always have exactly `levels` entries."""
        for n_levels in [1, 3, 5, 10]:
            grid = GridShape(levels=n_levels, step_bps=10.0)
            schedule = default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("0.25"),
                grid_shape=grid,
                price=Decimal("50000"),
            )
            assert len(schedule.qty_per_level) == n_levels


# --- Test Class: Risk Bound Verification ---


class TestRiskBound:
    """Tests verifying risk bounds are respected."""

    def test_worst_case_loss_within_budget(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Worst-case loss should not exceed dd_budget * equity."""
        equity = Decimal("10000")
        dd_budget = Decimal("0.20")
        max_loss = equity * dd_budget  # $2000

        schedule = default_sizer.compute(
            equity=equity,
            dd_budget=dd_budget,
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Worst-case loss should be <= budget
        assert schedule.worst_case_loss <= max_loss

        # Risk utilization should be <= 1.0
        assert schedule.risk_utilization <= Decimal("1.0")

    def test_rounding_preserves_risk_bound(self, standard_grid: GridShape) -> None:
        """Rounding should not cause risk bound violation."""
        # Use config with aggressive rounding (only 2 decimal places)
        config = AutoSizerConfig(qty_precision=2)
        sizer = AutoSizer(config)

        equity = Decimal("10000")
        dd_budget = Decimal("0.20")
        max_loss = equity * dd_budget

        schedule = sizer.compute(
            equity=equity,
            dd_budget=dd_budget,
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Even with aggressive rounding, should not exceed budget
        # (we round DOWN, so this should always hold)
        assert schedule.worst_case_loss <= max_loss
        assert schedule.risk_utilization <= Decimal("1.0")

    def test_risk_utilization_close_to_one(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Risk utilization should be close to 1.0 (efficient use of budget)."""
        schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # Should utilize most of the budget (> 90%)
        # But not exceed 100%
        assert schedule.risk_utilization > Decimal("0.90")
        assert schedule.risk_utilization <= Decimal("1.0")

    def test_min_qty_causes_underutilization(self) -> None:
        """When quantities fall below min_qty, risk utilization drops."""
        # Config with high min_qty
        config = AutoSizerConfig(min_qty=Decimal("1.0"))
        sizer = AutoSizer(config)

        # Small equity with expensive asset → qty would be < 1.0
        grid = GridShape(levels=5, step_bps=10.0)
        schedule = sizer.compute(
            equity=Decimal("1000"),  # Small account
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=grid,
            price=Decimal("50000"),  # BTC price
        )

        # All levels should be zeroed out (qty < min_qty)
        assert all(q == Decimal("0") for q in schedule.qty_per_level)
        assert schedule.effective_levels == 0
        assert schedule.risk_utilization == Decimal("0")


# --- Test Class: Sizing Modes ---


class TestSizingModes:
    """Tests for different sizing distribution modes."""

    def test_uniform_mode_equal_quantities(self, standard_grid: GridShape) -> None:
        """Uniform mode should produce equal quantities at each level."""
        config = AutoSizerConfig(sizing_mode=SizingMode.UNIFORM)
        sizer = AutoSizer(config)

        schedule = sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        # All non-zero quantities should be equal
        non_zero = [q for q in schedule.qty_per_level if q > 0]
        assert len(set(non_zero)) == 1  # All same value
        assert schedule.sizing_mode == SizingMode.UNIFORM

    def test_pyramid_mode_increasing_quantities(self) -> None:
        """Pyramid mode should have larger quantities at outer levels."""
        config = AutoSizerConfig(sizing_mode=SizingMode.PYRAMID)
        sizer = AutoSizer(config)
        grid = GridShape(levels=5, step_bps=10.0)

        schedule = sizer.compute(
            equity=Decimal("100000"),  # Large equity for clear differentiation
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=grid,
            price=Decimal("50000"),
        )

        # Each level should be >= previous (pyramid increases outward)
        for i in range(len(schedule.qty_per_level) - 1):
            assert schedule.qty_per_level[i + 1] >= schedule.qty_per_level[i]

        # Outer level should be significantly larger than inner
        assert schedule.qty_per_level[-1] > schedule.qty_per_level[0]
        assert schedule.sizing_mode == SizingMode.PYRAMID

    def test_inverse_pyramid_mode_decreasing_quantities(self) -> None:
        """Inverse pyramid should have smaller quantities at outer levels."""
        config = AutoSizerConfig(sizing_mode=SizingMode.INVERSE_PYRAMID)
        sizer = AutoSizer(config)
        grid = GridShape(levels=5, step_bps=10.0)

        schedule = sizer.compute(
            equity=Decimal("100000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=grid,
            price=Decimal("50000"),
        )

        # Each level should be <= previous (inverse pyramid decreases outward)
        for i in range(len(schedule.qty_per_level) - 1):
            assert schedule.qty_per_level[i + 1] <= schedule.qty_per_level[i]

        # Inner level should be larger than outer
        assert schedule.qty_per_level[0] > schedule.qty_per_level[-1]
        assert schedule.sizing_mode == SizingMode.INVERSE_PYRAMID


# --- Test Class: Edge Cases and Validation ---


class TestEdgeCasesAndValidation:
    """Tests for edge cases and input validation."""

    def test_zero_equity_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Zero equity should raise SizingError."""
        with pytest.raises(SizingError, match="equity must be > 0"):
            default_sizer.compute(
                equity=Decimal("0"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_negative_equity_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Negative equity should raise SizingError."""
        with pytest.raises(SizingError, match="equity must be > 0"):
            default_sizer.compute(
                equity=Decimal("-1000"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_zero_dd_budget_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Zero drawdown budget should raise SizingError."""
        with pytest.raises(SizingError, match="dd_budget must be > 0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("0"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_negative_dd_budget_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Negative drawdown budget should raise SizingError."""
        with pytest.raises(SizingError, match="dd_budget must be > 0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("-0.20"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_dd_budget_over_100_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Drawdown budget > 100% should raise SizingError."""
        with pytest.raises(SizingError, match=r"dd_budget must be <= 1\.0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("1.5"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_zero_adverse_move_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Zero adverse move should raise SizingError."""
        with pytest.raises(SizingError, match="adverse_move must be > 0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("0"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_negative_adverse_move_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Negative adverse move should raise SizingError."""
        with pytest.raises(SizingError, match="adverse_move must be > 0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("-0.25"),
                grid_shape=standard_grid,
                price=Decimal("50000"),
            )

    def test_zero_price_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Zero price should raise SizingError."""
        with pytest.raises(SizingError, match="price must be > 0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("0"),
            )

    def test_negative_price_raises_error(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Negative price should raise SizingError."""
        with pytest.raises(SizingError, match="price must be > 0"):
            default_sizer.compute(
                equity=Decimal("10000"),
                dd_budget=Decimal("0.20"),
                adverse_move=Decimal("0.25"),
                grid_shape=standard_grid,
                price=Decimal("-50000"),
            )

    def test_zero_levels_raises_error(self) -> None:
        """Zero levels in grid shape should raise SizingError."""
        with pytest.raises(SizingError, match="levels must be >= 1"):
            GridShape(levels=0, step_bps=10.0)

    def test_negative_levels_raises_error(self) -> None:
        """Negative levels in grid shape should raise SizingError."""
        with pytest.raises(SizingError, match="levels must be >= 1"):
            GridShape(levels=-1, step_bps=10.0)

    def test_zero_step_bps_raises_error(self) -> None:
        """Zero step_bps should raise SizingError."""
        with pytest.raises(SizingError, match="step_bps must be > 0"):
            GridShape(levels=5, step_bps=0)

    def test_negative_step_bps_raises_error(self) -> None:
        """Negative step_bps should raise SizingError."""
        with pytest.raises(SizingError, match="step_bps must be > 0"):
            GridShape(levels=5, step_bps=-10.0)

    def test_negative_top_k_raises_error(self) -> None:
        """Negative top_k should raise SizingError."""
        with pytest.raises(SizingError, match="top_k must be >= 0"):
            GridShape(levels=5, step_bps=10.0, top_k=-1)


# --- Test Class: Determinism ---


class TestDeterminism:
    """Tests verifying deterministic behavior."""

    def test_same_inputs_same_output(
        self, default_sizer: AutoSizer, standard_grid: GridShape
    ) -> None:
        """Same inputs should always produce same outputs."""
        schedule1 = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )
        schedule2 = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        assert schedule1.qty_per_level == schedule2.qty_per_level
        assert schedule1.total_notional == schedule2.total_notional
        assert schedule1.worst_case_loss == schedule2.worst_case_loss
        assert schedule1.risk_utilization == schedule2.risk_utilization

    def test_deterministic_across_instances(self, standard_grid: GridShape) -> None:
        """Different AutoSizer instances with same config produce same results."""
        config = AutoSizerConfig()
        sizer1 = AutoSizer(config)
        sizer2 = AutoSizer(config)

        schedule1 = sizer1.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )
        schedule2 = sizer2.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        assert schedule1.qty_per_level == schedule2.qty_per_level
        assert schedule1.total_notional == schedule2.total_notional

    def test_to_dict_serializable(self, default_sizer: AutoSizer, standard_grid: GridShape) -> None:
        """SizeSchedule should be JSON-serializable via to_dict."""
        schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=standard_grid,
            price=Decimal("50000"),
        )

        d = schedule.to_dict()

        # All values should be strings or primitives
        assert isinstance(d["qty_per_level"], list)
        assert all(isinstance(q, str) for q in d["qty_per_level"])
        assert isinstance(d["total_notional"], str)
        assert isinstance(d["worst_case_loss"], str)
        assert isinstance(d["risk_utilization"], str)
        assert isinstance(d["effective_levels"], int)
        assert isinstance(d["sizing_mode"], str)


# --- Test Class: Single Level Edge Case ---


class TestSingleLevel:
    """Tests for single-level grid edge case."""

    def test_single_level_grid(self, default_sizer: AutoSizer) -> None:
        """Single level grid should work correctly."""
        grid = GridShape(levels=1, step_bps=10.0)

        schedule = default_sizer.compute(
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            grid_shape=grid,
            price=Decimal("50000"),
        )

        assert len(schedule.qty_per_level) == 1
        assert schedule.effective_levels == 1
        assert schedule.qty_per_level[0] > Decimal("0")


# --- Test Class: Config Validation ---


class TestConfigValidation:
    """Tests for AutoSizerConfig validation."""

    def test_negative_min_qty_raises_error(self) -> None:
        """Negative min_qty should raise SizingError."""
        with pytest.raises(SizingError, match="min_qty must be >= 0"):
            AutoSizerConfig(min_qty=Decimal("-0.001"))

    def test_negative_qty_precision_raises_error(self) -> None:
        """Negative qty_precision should raise SizingError."""
        with pytest.raises(SizingError, match="qty_precision must be >= 0"):
            AutoSizerConfig(qty_precision=-1)

    def test_zero_max_risk_utilization_raises_error(self) -> None:
        """Zero max_risk_utilization should raise SizingError."""
        with pytest.raises(SizingError, match="max_risk_utilization must be > 0"):
            AutoSizerConfig(max_risk_utilization=Decimal("0"))

    def test_negative_max_risk_utilization_raises_error(self) -> None:
        """Negative max_risk_utilization should raise SizingError."""
        with pytest.raises(SizingError, match="max_risk_utilization must be > 0"):
            AutoSizerConfig(max_risk_utilization=Decimal("-0.5"))
