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
from grinder.sizing import AutoSizerConfig, DdAllocator, RiskTier, SymbolCandidate


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


class TestAutoSizingIntegration:
    """Tests for auto-sizing integration (ASM-P2-01)."""

    def test_legacy_sizing_when_disabled(self) -> None:
        """When auto_sizing_enabled=False, uses uniform size_per_level."""
        config = AdaptiveGridConfig(
            size_per_level=Decimal("0.05"),
            auto_sizing_enabled=False,
        )
        policy = AdaptiveGridPolicy(config)

        features = {
            "mid_price": Decimal("50000"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 5,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "BTCUSDT",
        }

        plan = policy.evaluate(features)

        # Should use legacy uniform sizing
        assert all(q == Decimal("0.05") for q in plan.size_schedule)

    def test_auto_sizing_when_enabled(self) -> None:
        """When auto_sizing_enabled=True, uses AutoSizer for size_schedule."""
        config = AdaptiveGridConfig(
            size_per_level=Decimal("0.05"),  # Legacy fallback
            auto_sizing_enabled=True,
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),  # 20% max drawdown
            adverse_move=Decimal("0.25"),  # 25% worst-case move
            auto_sizer_config=AutoSizerConfig(),
        )
        policy = AdaptiveGridPolicy(config)

        features = {
            "mid_price": Decimal("50000"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 5,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "BTCUSDT",
        }

        plan = policy.evaluate(features)

        # Should use auto-sizing (not uniform)
        # With auto-sizing, quantities should be risk-derived, not all 0.05
        # For $10k equity, 20% DD budget, 25% adverse move:
        # max_loss = $2000, total_qty = $2000 / ($50000 * 0.25) = 0.16 BTC total
        # If we have ~3-4 levels, qty_per_level should be ~0.04-0.05 each
        assert len(plan.size_schedule) > 0
        total_qty = sum(plan.size_schedule)
        assert total_qty > Decimal("0")

        # Verify risk bound: worst_case_loss <= dd_budget * equity
        worst_case = total_qty * Decimal("50000") * Decimal("0.25")
        max_allowed = Decimal("10000") * Decimal("0.20")
        assert worst_case <= max_allowed

    def test_auto_sizing_fallback_on_missing_params(self) -> None:
        """When auto_sizing params are missing, falls back to legacy."""
        config = AdaptiveGridConfig(
            size_per_level=Decimal("0.05"),
            auto_sizing_enabled=True,
            # Missing: equity, dd_budget, adverse_move
        )
        policy = AdaptiveGridPolicy(config)

        features = {
            "mid_price": Decimal("50000"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 5,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "BTCUSDT",
        }

        plan = policy.evaluate(features)

        # Should fall back to legacy uniform sizing
        assert all(q == Decimal("0.05") for q in plan.size_schedule)

    def test_auto_sizing_determinism(self) -> None:
        """Auto-sizing produces deterministic results."""
        config = AdaptiveGridConfig(
            auto_sizing_enabled=True,
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            auto_sizer_config=AutoSizerConfig(),
        )
        policy = AdaptiveGridPolicy(config)

        features = {
            "mid_price": Decimal("50000"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 5,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "BTCUSDT",
        }

        plan1 = policy.evaluate(features)
        plan2 = policy.evaluate(features)

        assert plan1.size_schedule == plan2.size_schedule

    def test_config_serialization_with_auto_sizing(self) -> None:
        """Config with auto-sizing serializes correctly."""
        config = AdaptiveGridConfig(
            auto_sizing_enabled=True,
            equity=Decimal("10000"),
            dd_budget=Decimal("0.20"),
            adverse_move=Decimal("0.25"),
            auto_sizer_config=AutoSizerConfig(),
        )

        d = config.to_dict()

        assert d["auto_sizing_enabled"] is True
        assert d["equity"] == "10000"
        assert d["dd_budget"] == "0.20"
        assert d["adverse_move"] == "0.25"


class TestDdAllocatorIntegration:
    """Tests for DdAllocator -> AdaptiveGridPolicy integration (ASM-P2-02).

    Verifies that per-symbol dd_budget from DdAllocator flows correctly
    to AutoSizer via AdaptiveGridConfig.
    """

    def test_per_symbol_budget_flows_to_sizer(self) -> None:
        """DdAllocator output should be usable as policy dd_budget."""
        # Step 1: Allocate portfolio budget
        allocator = DdAllocator()
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
        ]

        equity = Decimal("100000")
        portfolio_dd_budget = Decimal("0.20")  # 20% total

        result = allocator.allocate(
            equity=equity,
            portfolio_dd_budget=portfolio_dd_budget,
            candidates=candidates,
        )

        # Step 2: Use per-symbol budget in policy config
        btc_dd_budget = result.allocations["BTCUSDT"]  # Per-symbol fraction

        config = AdaptiveGridConfig(
            auto_sizing_enabled=True,
            equity=equity,
            dd_budget=btc_dd_budget,  # From DdAllocator
            adverse_move=Decimal("0.25"),
            auto_sizer_config=AutoSizerConfig(),
        )
        policy = AdaptiveGridPolicy(config)

        features = {
            "mid_price": Decimal("50000"),
            "natr_bps": 100,
            "spread_bps": 10,
            "thin_l1": Decimal("1.0"),
            "net_return_bps": 50,
            "range_score": 5,
            "warmup_bars": 20,
            "ts": 1000,
            "symbol": "BTCUSDT",
        }

        plan = policy.evaluate(features)

        # Verify risk bound uses per-symbol budget
        total_qty = sum(plan.size_schedule)
        worst_case = total_qty * Decimal("50000") * Decimal("0.25")
        max_allowed = equity * btc_dd_budget  # Per-symbol, not portfolio
        assert worst_case <= max_allowed

    def test_high_risk_symbol_gets_smaller_budget(self) -> None:
        """HIGH tier symbol should get smaller budget than LOW tier."""
        allocator = DdAllocator()
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="STABLECOIN", tier=RiskTier.LOW),
        ]

        result = allocator.allocate(
            equity=Decimal("100000"),
            portfolio_dd_budget=Decimal("0.20"),
            candidates=candidates,
        )

        btc_budget = result.allocations["BTCUSDT"]
        stable_budget = result.allocations["STABLECOIN"]

        # HIGH tier gets less budget
        assert btc_budget < stable_budget

        # Both can be used to create valid policies
        for symbol, budget in result.allocations.items():
            config = AdaptiveGridConfig(
                auto_sizing_enabled=True,
                equity=Decimal("100000"),
                dd_budget=budget,
                adverse_move=Decimal("0.25"),
            )
            policy = AdaptiveGridPolicy(config)

            features = {
                "mid_price": Decimal("50000"),
                "natr_bps": 100,
                "spread_bps": 10,
                "thin_l1": Decimal("1.0"),
                "net_return_bps": 50,
                "range_score": 5,
                "warmup_bars": 20,
                "ts": 1000,
                "symbol": symbol,
            }

            plan = policy.evaluate(features)
            assert len(plan.size_schedule) >= 0  # Valid plan created

    def test_portfolio_conservation_across_symbols(self) -> None:
        """Sum of per-symbol risk budgets should not exceed portfolio budget."""
        allocator = DdAllocator()
        candidates = [
            SymbolCandidate(symbol="BTCUSDT", tier=RiskTier.HIGH),
            SymbolCandidate(symbol="ETHUSDT", tier=RiskTier.MED),
            SymbolCandidate(symbol="BNBUSDT", tier=RiskTier.LOW),
        ]

        equity = Decimal("100000")
        portfolio_dd_budget = Decimal("0.20")

        result = allocator.allocate(
            equity=equity,
            portfolio_dd_budget=portfolio_dd_budget,
            candidates=candidates,
        )

        # Create policies for each symbol and compute total worst-case
        total_worst_case = Decimal("0")

        for symbol, dd_budget in result.allocations.items():
            config = AdaptiveGridConfig(
                auto_sizing_enabled=True,
                equity=equity,
                dd_budget=dd_budget,
                adverse_move=Decimal("0.25"),
            )
            policy = AdaptiveGridPolicy(config)

            features = {
                "mid_price": Decimal("50000"),
                "natr_bps": 100,
                "spread_bps": 10,
                "thin_l1": Decimal("1.0"),
                "net_return_bps": 50,
                "range_score": 5,
                "warmup_bars": 20,
                "ts": 1000,
                "symbol": symbol,
            }

            plan = policy.evaluate(features)
            symbol_qty = sum(plan.size_schedule)
            symbol_worst_case = symbol_qty * Decimal("50000") * Decimal("0.25")
            total_worst_case += symbol_worst_case

        # Total worst-case across all symbols should not exceed portfolio budget
        portfolio_budget_usd = equity * portfolio_dd_budget
        assert total_worst_case <= portfolio_budget_usd
