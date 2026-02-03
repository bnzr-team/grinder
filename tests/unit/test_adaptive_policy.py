"""Unit tests for AdaptiveGridPolicy v1 (ASM-P1-05).

These tests verify the deterministic computation of:
- step_bps from NATR and regime
- width_bps from X_stress model
- levels from width/step with clamps

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.8-17.10, ADR-022
"""

from __future__ import annotations

from decimal import Decimal

from grinder.controller.regime import Regime
from grinder.core import GridMode, MarketRegime, ResetAction
from grinder.policies.grid.adaptive import (
    AdaptiveGridConfig,
    AdaptiveGridPolicy,
    compute_levels,
    compute_step_bps,
    compute_width_bps,
)


class TestComputeStepBps:
    """Tests for compute_step_bps function."""

    def test_step_floor_applied(self) -> None:
        """Step is at least step_min_bps."""
        config = AdaptiveGridConfig(step_min_bps=5, step_alpha=30)

        # Very low NATR should hit floor
        step = compute_step_bps(natr_bps=1, regime=Regime.RANGE, config=config)
        assert step == 5  # Floor

    def test_step_from_natr_range(self) -> None:
        """Step computed from NATR in RANGE regime."""
        config = AdaptiveGridConfig(step_min_bps=5, step_alpha=30)

        # NATR=100 bps, alpha=0.30, regime_mult=1.0
        # step = 0.30 * 100 * 1.0 = 30 bps
        step = compute_step_bps(natr_bps=100, regime=Regime.RANGE, config=config)
        assert step == 30

    def test_step_from_natr_vol_shock(self) -> None:
        """Step increased in VOL_SHOCK regime."""
        config = AdaptiveGridConfig(step_min_bps=5, step_alpha=30, vol_shock_step_mult=150)

        # NATR=100 bps, alpha=0.30, regime_mult=1.50
        # step = 0.30 * 100 * 1.50 = 45 bps
        step = compute_step_bps(natr_bps=100, regime=Regime.VOL_SHOCK, config=config)
        assert step == 45

    def test_step_from_natr_thin_book(self) -> None:
        """Step increased further in THIN_BOOK regime."""
        config = AdaptiveGridConfig(step_min_bps=5, step_alpha=30, thin_book_step_mult=200)

        # NATR=100 bps, alpha=0.30, regime_mult=2.00
        # step = 0.30 * 100 * 2.00 = 60 bps
        step = compute_step_bps(natr_bps=100, regime=Regime.THIN_BOOK, config=config)
        assert step == 60

    def test_step_deterministic(self) -> None:
        """Same inputs produce same step."""
        config = AdaptiveGridConfig()

        step1 = compute_step_bps(natr_bps=150, regime=Regime.RANGE, config=config)
        step2 = compute_step_bps(natr_bps=150, regime=Regime.RANGE, config=config)
        assert step1 == step2


class TestComputeWidthBps:
    """Tests for compute_width_bps function."""

    def test_width_symmetric_in_range(self) -> None:
        """Width is symmetric in RANGE regime."""
        config = AdaptiveGridConfig(
            horizon_minutes=60,
            bar_interval_minutes=1,
            k_tail=200,
            x_min_bps=20,
            x_cap_bps=500,
        )

        width_up, width_down = compute_width_bps(natr_bps=100, regime=Regime.RANGE, config=config)
        assert width_up == width_down

    def test_width_asymmetric_trend_up(self) -> None:
        """Width is asymmetric in TREND_UP (more width on up/sell side)."""
        config = AdaptiveGridConfig(
            horizon_minutes=60,
            bar_interval_minutes=1,
            k_tail=200,
            x_min_bps=20,
            x_cap_bps=500,
            trend_width_mult=130,
        )

        width_up, width_down = compute_width_bps(
            natr_bps=100, regime=Regime.TREND_UP, config=config
        )
        # In uptrend, more width on up (sell) side
        assert width_up > width_down

    def test_width_asymmetric_trend_down(self) -> None:
        """Width is asymmetric in TREND_DOWN (more width on down/buy side)."""
        config = AdaptiveGridConfig(
            horizon_minutes=60,
            bar_interval_minutes=1,
            k_tail=200,
            x_min_bps=20,
            x_cap_bps=500,
            trend_width_mult=130,
        )

        width_up, width_down = compute_width_bps(
            natr_bps=100, regime=Regime.TREND_DOWN, config=config
        )
        # In downtrend, more width on down (buy) side
        assert width_down > width_up

    def test_width_clamped_to_min(self) -> None:
        """Width is clamped to x_min_bps."""
        config = AdaptiveGridConfig(
            horizon_minutes=1,
            bar_interval_minutes=1,
            k_tail=100,
            x_min_bps=50,
            x_cap_bps=500,
        )

        # Very low NATR should hit floor
        width_up, width_down = compute_width_bps(natr_bps=1, regime=Regime.RANGE, config=config)
        assert width_up >= 50
        assert width_down >= 50

    def test_width_clamped_to_cap(self) -> None:
        """Width is clamped to x_cap_bps."""
        config = AdaptiveGridConfig(
            horizon_minutes=60,
            bar_interval_minutes=1,
            k_tail=500,
            x_min_bps=20,
            x_cap_bps=200,
        )

        # High NATR with high k_tail should hit cap
        width_up, width_down = compute_width_bps(natr_bps=500, regime=Regime.RANGE, config=config)
        assert width_up <= 200
        assert width_down <= 200

    def test_width_horizon_scaling(self) -> None:
        """Width scales with sqrt(horizon)."""
        config_short = AdaptiveGridConfig(
            horizon_minutes=15,
            bar_interval_minutes=1,
            k_tail=200,
            x_min_bps=20,
            x_cap_bps=1000,
        )
        config_long = AdaptiveGridConfig(
            horizon_minutes=60,
            bar_interval_minutes=1,
            k_tail=200,
            x_min_bps=20,
            x_cap_bps=1000,
        )

        width_short, _ = compute_width_bps(natr_bps=100, regime=Regime.RANGE, config=config_short)
        width_long, _ = compute_width_bps(natr_bps=100, regime=Regime.RANGE, config=config_long)

        # Longer horizon = wider grid (sqrt(60) / sqrt(15) = 2)
        assert width_long > width_short


class TestComputeLevels:
    """Tests for compute_levels function."""

    def test_levels_from_width_step(self) -> None:
        """Levels = ceil(width / step)."""
        config = AdaptiveGridConfig(levels_min=2, levels_max=20)

        # width=100, step=30 → ceil(100/30) = 4
        levels_up, levels_down = compute_levels(
            width_up_bps=100, width_down_bps=100, step_bps=30, config=config
        )
        assert levels_up == 4
        assert levels_down == 4

    def test_levels_clamped_to_min(self) -> None:
        """Levels are at least levels_min."""
        config = AdaptiveGridConfig(levels_min=3, levels_max=20)

        # width=10, step=30 → ceil(10/30) = 1, but clamped to 3
        levels_up, levels_down = compute_levels(
            width_up_bps=10, width_down_bps=10, step_bps=30, config=config
        )
        assert levels_up == 3
        assert levels_down == 3

    def test_levels_clamped_to_max(self) -> None:
        """Levels are at most levels_max."""
        config = AdaptiveGridConfig(levels_min=2, levels_max=10)

        # width=500, step=10 → ceil(500/10) = 50, but clamped to 10
        levels_up, levels_down = compute_levels(
            width_up_bps=500, width_down_bps=500, step_bps=10, config=config
        )
        assert levels_up == 10
        assert levels_down == 10

    def test_levels_asymmetric(self) -> None:
        """Different widths produce different level counts."""
        config = AdaptiveGridConfig(levels_min=2, levels_max=20)

        levels_up, levels_down = compute_levels(
            width_up_bps=120, width_down_bps=80, step_bps=30, config=config
        )
        # 120/30 = 4, 80/30 = 3 (rounded up)
        assert levels_up == 4
        assert levels_down == 3


class TestAdaptiveGridPolicy:
    """Tests for AdaptiveGridPolicy class."""

    def test_evaluate_returns_grid_plan(self) -> None:
        """Evaluate returns a valid GridPlan."""
        policy = AdaptiveGridPolicy()
        features = {
            "mid_price": Decimal("100"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 10,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "TESTUSDT",
        }

        plan = policy.evaluate(features)

        assert plan.mode == GridMode.BILATERAL
        assert plan.center_price == Decimal("100")
        assert plan.spacing_bps > 0
        assert plan.levels_up > 0
        assert plan.levels_down > 0
        assert len(plan.size_schedule) > 0

    def test_evaluate_pause_on_emergency(self) -> None:
        """Emergency kill-switch results in pause plan."""
        policy = AdaptiveGridPolicy()
        features = {
            "mid_price": Decimal("100"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 10,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "TESTUSDT",
        }

        plan = policy.evaluate(features, kill_switch_active=True)

        assert plan.levels_up == 0
        assert plan.levels_down == 0
        assert plan.reset_action == ResetAction.HARD
        assert "REGIME_EMERGENCY" in plan.reason_codes

    def test_evaluate_regime_in_plan(self) -> None:
        """Plan regime matches classified regime."""
        policy = AdaptiveGridPolicy()
        features = {
            "mid_price": Decimal("100"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 10,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "TESTUSDT",
        }

        plan = policy.evaluate(features)

        # Normal conditions should be RANGE
        assert plan.regime == MarketRegime.RANGE
        assert "REGIME_RANGE" in plan.reason_codes

    def test_evaluate_without_features_uses_defaults(self) -> None:
        """Missing features use safe defaults."""
        policy = AdaptiveGridPolicy()
        features = {
            "mid_price": Decimal("100"),
        }

        plan = policy.evaluate(features)

        # Should still produce a valid plan with defaults
        assert plan.mode == GridMode.BILATERAL
        assert plan.spacing_bps >= policy.config.step_min_bps

    def test_should_activate_with_features(self) -> None:
        """Policy activates when natr_bps is available."""
        policy = AdaptiveGridPolicy()

        assert policy.should_activate({"natr_bps": 100})
        assert not policy.should_activate({"mid_price": 100})

    def test_config_to_dict(self) -> None:
        """Config serializes correctly."""
        config = AdaptiveGridConfig(
            step_min_bps=10,
            vol_shock_step_mult=180,
        )

        d = config.to_dict()

        assert d["step_min_bps"] == 10
        assert d["vol_shock_step_mult"] == 180


class TestAdaptiveGridPolicyDeterminism:
    """Tests for deterministic behavior."""

    def test_same_inputs_same_outputs(self) -> None:
        """Same feature inputs produce identical GridPlan."""
        policy = AdaptiveGridPolicy()
        features = {
            "mid_price": Decimal("100"),
            "natr_bps": 150,
            "spread_bps": 15,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 75,
            "range_score": 8,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "TESTUSDT",
        }

        plan1 = policy.evaluate(features)
        plan2 = policy.evaluate(features)

        assert plan1.spacing_bps == plan2.spacing_bps
        assert plan1.width_bps == plan2.width_bps
        assert plan1.levels_up == plan2.levels_up
        assert plan1.levels_down == plan2.levels_down
        assert plan1.regime == plan2.regime

    def test_integer_outputs(self) -> None:
        """All intermediate calculations produce integer bps."""
        config = AdaptiveGridConfig()

        step = compute_step_bps(natr_bps=123, regime=Regime.RANGE, config=config)
        assert isinstance(step, int)

        width_up, width_down = compute_width_bps(natr_bps=123, regime=Regime.RANGE, config=config)
        assert isinstance(width_up, int)
        assert isinstance(width_down, int)

        levels_up, levels_down = compute_levels(
            width_up_bps=100, width_down_bps=100, step_bps=30, config=config
        )
        assert isinstance(levels_up, int)
        assert isinstance(levels_down, int)
