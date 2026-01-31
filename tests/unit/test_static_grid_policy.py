"""Tests for StaticGridPolicy v0.

Tests verify the requirements from docs/ROADMAP.md DoD for PR-016:
- symmetry: levels_up == levels_down
- center == mid
- spacing/levels/width invariants
- reason_codes not empty, contains expected
- deterministic output for same input
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from grinder.core import GridMode, MarketRegime, ResetAction
from grinder.policies.grid.static import StaticGridPolicy


class TestStaticGridPolicySymmetry:
    """Tests for symmetric grid property."""

    def test_levels_equal(self) -> None:
        """Test levels_up equals levels_down (symmetry)."""
        policy = StaticGridPolicy(spacing_bps=10.0, levels=5)
        features = {"mid_price": Decimal("50000")}

        plan = policy.evaluate(features)

        assert plan.levels_up == plan.levels_down
        assert plan.levels_up == 5

    def test_symmetric_with_different_levels(self) -> None:
        """Test symmetry holds for various level counts."""
        for levels in [1, 3, 5, 10]:
            policy = StaticGridPolicy(levels=levels)
            features = {"mid_price": Decimal("1000")}

            plan = policy.evaluate(features)

            assert plan.levels_up == plan.levels_down == levels


class TestStaticGridPolicyCenter:
    """Tests for center == mid price."""

    def test_center_equals_mid(self) -> None:
        """Test center_price equals mid_price from features."""
        policy = StaticGridPolicy()
        mid = Decimal("50000.50")
        features = {"mid_price": mid}

        plan = policy.evaluate(features)

        assert plan.center_price == mid

    def test_center_with_float_input(self) -> None:
        """Test center handles float mid_price input."""
        policy = StaticGridPolicy()
        features = {"mid_price": 50000.5}

        plan = policy.evaluate(features)

        assert plan.center_price == Decimal("50000.5")


class TestStaticGridPolicyInvariants:
    """Tests for spacing/levels/width invariants."""

    def test_spacing_matches_config(self) -> None:
        """Test spacing_bps matches configured value."""
        policy = StaticGridPolicy(spacing_bps=8.0)
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert plan.spacing_bps == 8.0

    def test_width_calculation(self) -> None:
        """Test width_bps = spacing_bps * levels."""
        policy = StaticGridPolicy(spacing_bps=10.0, levels=5)
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        expected_width = 10.0 * 5  # 50 bps
        assert plan.width_bps == expected_width

    def test_width_calculation_various(self) -> None:
        """Test width calculation for various configs."""
        test_cases = [
            (10.0, 5, 50.0),
            (8.0, 3, 24.0),
            (15.0, 10, 150.0),
            (5.0, 1, 5.0),
        ]
        for spacing, levels, expected_width in test_cases:
            policy = StaticGridPolicy(spacing_bps=spacing, levels=levels)
            features = {"mid_price": Decimal("1000")}

            plan = policy.evaluate(features)

            assert plan.width_bps == expected_width, (
                f"Failed for spacing={spacing}, levels={levels}"
            )

    def test_size_schedule_uniform(self) -> None:
        """Test size_schedule has uniform sizes."""
        size = Decimal("150")
        policy = StaticGridPolicy(levels=5, size_per_level=size)
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert len(plan.size_schedule) == 5
        assert all(s == size for s in plan.size_schedule)


class TestStaticGridPolicyReasonCodes:
    """Tests for reason_codes field."""

    def test_reason_codes_not_empty(self) -> None:
        """Test reason_codes is not empty."""
        policy = StaticGridPolicy()
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert plan.reason_codes
        assert len(plan.reason_codes) > 0

    def test_reason_codes_contains_regime_range(self) -> None:
        """Test reason_codes contains REGIME_RANGE."""
        policy = StaticGridPolicy()
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert "REGIME_RANGE" in plan.reason_codes


class TestStaticGridPolicyMode:
    """Tests for grid mode."""

    def test_mode_bilateral(self) -> None:
        """Test mode is always BILATERAL."""
        policy = StaticGridPolicy()
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert plan.mode == GridMode.BILATERAL

    def test_regime_range(self) -> None:
        """Test regime is always RANGE."""
        policy = StaticGridPolicy()
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert plan.regime == MarketRegime.RANGE

    def test_reset_action_none(self) -> None:
        """Test reset_action is always NONE."""
        policy = StaticGridPolicy()
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert plan.reset_action == ResetAction.NONE

    def test_skew_zero(self) -> None:
        """Test skew is always zero (no inventory adjustment)."""
        policy = StaticGridPolicy()
        features = {"mid_price": Decimal("1000")}

        plan = policy.evaluate(features)

        assert plan.skew_bps == 0.0


class TestStaticGridPolicyDeterminism:
    """Tests for deterministic output."""

    def test_same_input_same_output(self) -> None:
        """Test same input produces identical output."""
        policy = StaticGridPolicy(spacing_bps=10.0, levels=5)
        features = {"mid_price": Decimal("50000")}

        plan1 = policy.evaluate(features)
        plan2 = policy.evaluate(features)

        assert plan1.center_price == plan2.center_price
        assert plan1.spacing_bps == plan2.spacing_bps
        assert plan1.levels_up == plan2.levels_up
        assert plan1.levels_down == plan2.levels_down
        assert plan1.width_bps == plan2.width_bps
        assert plan1.mode == plan2.mode
        assert plan1.regime == plan2.regime
        assert plan1.reset_action == plan2.reset_action
        assert plan1.reason_codes == plan2.reason_codes
        assert plan1.size_schedule == plan2.size_schedule

    def test_deterministic_across_instances(self) -> None:
        """Test two policy instances produce same output."""
        policy1 = StaticGridPolicy(spacing_bps=10.0, levels=5)
        policy2 = StaticGridPolicy(spacing_bps=10.0, levels=5)
        features = {"mid_price": Decimal("50000")}

        plan1 = policy1.evaluate(features)
        plan2 = policy2.evaluate(features)

        assert plan1.center_price == plan2.center_price
        assert plan1.spacing_bps == plan2.spacing_bps
        assert plan1.levels_up == plan2.levels_up
        assert plan1.width_bps == plan2.width_bps


class TestStaticGridPolicyShouldActivate:
    """Tests for should_activate method."""

    def test_always_active(self) -> None:
        """Test policy is always active (fallback policy)."""
        policy = StaticGridPolicy()

        assert policy.should_activate({}) is True
        assert policy.should_activate({"anything": 123}) is True


class TestStaticGridPolicyValidation:
    """Tests for input validation."""

    def test_requires_mid_price(self) -> None:
        """Test evaluate raises if mid_price missing."""
        policy = StaticGridPolicy()

        with pytest.raises(KeyError, match="mid_price"):
            policy.evaluate({})

    def test_invalid_spacing_rejected(self) -> None:
        """Test constructor rejects invalid spacing."""
        with pytest.raises(ValueError, match="spacing_bps must be positive"):
            StaticGridPolicy(spacing_bps=0)

        with pytest.raises(ValueError, match="spacing_bps must be positive"):
            StaticGridPolicy(spacing_bps=-5)

    def test_invalid_levels_rejected(self) -> None:
        """Test constructor rejects invalid levels."""
        with pytest.raises(ValueError, match="levels must be positive"):
            StaticGridPolicy(levels=0)

        with pytest.raises(ValueError, match="levels must be positive"):
            StaticGridPolicy(levels=-1)

    def test_invalid_size_rejected(self) -> None:
        """Test constructor rejects invalid size."""
        with pytest.raises(ValueError, match="size_per_level must be positive"):
            StaticGridPolicy(size_per_level=Decimal("0"))

        with pytest.raises(ValueError, match="size_per_level must be positive"):
            StaticGridPolicy(size_per_level=Decimal("-100"))


class TestStaticGridPolicyName:
    """Tests for policy name."""

    def test_policy_name(self) -> None:
        """Test policy has correct name."""
        policy = StaticGridPolicy()

        assert policy.name == "STATIC_GRID"
