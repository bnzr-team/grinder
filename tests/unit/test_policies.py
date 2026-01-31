"""Tests for policy base classes."""

from decimal import Decimal

import pytest

from grinder.core import GridMode, MarketRegime, ResetAction
from grinder.policies.base import GridPlan, GridPolicy


class TestGridPlan:
    """Tests for GridPlan dataclass."""

    def test_create_plan(self) -> None:
        """Test creating a grid plan."""
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=5.0,
            levels_up=3,
            levels_down=3,
            size_schedule=[Decimal("0.01"), Decimal("0.02"), Decimal("0.03")],
        )

        assert plan.mode == GridMode.BILATERAL
        assert plan.center_price == Decimal("50000")
        assert plan.spacing_bps == 5.0
        assert plan.levels_up == 3
        assert plan.levels_down == 3
        assert len(plan.size_schedule) == 3
        assert plan.skew_bps == 0.0  # default
        # New defaults
        assert plan.regime == MarketRegime.RANGE
        assert plan.reset_action == ResetAction.NONE
        assert plan.reason_codes == []
        assert plan.width_bps == 0.0

    def test_plan_with_skew(self) -> None:
        """Test plan with skew."""
        plan = GridPlan(
            mode=GridMode.UNI_LONG,
            center_price=Decimal("50000"),
            spacing_bps=5.0,
            levels_up=5,
            levels_down=0,
            size_schedule=[Decimal("0.01")],
            skew_bps=-2.0,
            reason_codes=["TREND_UP", "LOW_TOX"],
        )

        assert plan.mode == GridMode.UNI_LONG
        assert plan.skew_bps == -2.0
        assert plan.reason_codes == ["TREND_UP", "LOW_TOX"]

    def test_plan_with_new_fields(self) -> None:
        """Test plan with regime, width, reset fields."""
        plan = GridPlan(
            mode=GridMode.BILATERAL,
            center_price=Decimal("50000"),
            spacing_bps=10.0,
            levels_up=5,
            levels_down=5,
            size_schedule=[Decimal("100")],
            regime=MarketRegime.RANGE,
            width_bps=50.0,  # 10 bps * 5 levels
            reset_action=ResetAction.NONE,
            reason_codes=["REGIME_RANGE"],
        )

        assert plan.regime == MarketRegime.RANGE
        assert plan.width_bps == 50.0
        assert plan.reset_action == ResetAction.NONE
        assert plan.reason_codes == ["REGIME_RANGE"]


class TestGridPolicyInterface:
    """Tests for GridPolicy abstract class."""

    def test_cannot_instantiate_abstract(self) -> None:
        """Test that abstract class cannot be instantiated."""
        with pytest.raises(TypeError):
            GridPolicy()  # type: ignore
