"""NATR→spacing hook normative contract tests (TRD-3b).

Lock down the existing compute_step_bps formula as a regression contract:
- Monotonicity: higher natr_bps → higher (or equal) step_bps
- Bounds: step_bps >= step_min_bps (floor always holds)
- Warmup/zero: natr_bps=0 → step_bps == step_min_bps
- Determinism: same inputs → identical output
- Regime multiplier: higher regime_mult → higher step_bps (at fixed natr_bps)
- Integer math: all outputs are int

These are CONTRACT tests — if they break, the NATR→spacing formula changed.
See docs/24_NATR_SPACING_HOOK.md for the normative specification.

What these tests do NOT prove:
- Width/X_stress computation (covered by test_adaptive_policy.py::TestComputeWidthBps)
- Levels computation (covered by test_adaptive_policy.py::TestComputeLevels)
- Regime classification (covered by test_regime.py)
- NATR encoding (covered by test_natr_contract.py, TRD-3a)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from grinder.controller.regime import Regime
from grinder.policies.grid.adaptive import (
    AdaptiveGridConfig,
    AdaptiveGridPolicy,
    compute_step_bps,
)

# ---------------------------------------------------------------------------
# Default config for contract tests
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = AdaptiveGridConfig(
    step_min_bps=5,
    step_alpha=30,
    vol_shock_step_mult=150,
    thin_book_step_mult=200,
)


def _make_features(natr_bps: int = 100, **overrides: Any) -> dict[str, Any]:
    """Create a minimal features dict for AdaptiveGridPolicy.evaluate()."""
    base: dict[str, Any] = {
        "mid_price": Decimal("50000"),
        "natr_bps": natr_bps,
        "spread_bps": 10,
        "thin_l1": Decimal("1.0"),
        "net_return_bps": 50,
        "range_score": 10,
        "warmup_bars": 20,
        "ts": 1_000_000,
        "symbol": "BTCUSDT",
    }
    base.update(overrides)
    return base


# ===========================================================================
# A) Monotonicity
# ===========================================================================


class TestMonotonicity:
    """Verify that higher natr_bps produces higher (or equal) step_bps.

    SSOT formula (docs/24_NATR_SPACING_HOOK.md):
        step_raw = (step_alpha * natr_bps * regime_mult) // 10000
        step_bps = max(step_min_bps, step_raw)

    Since step_alpha > 0 and regime_mult > 0, step_raw is monotonically
    non-decreasing in natr_bps. After the floor, step_bps is also
    monotonically non-decreasing.
    """

    @pytest.mark.parametrize(
        "natr_low,natr_high",
        [
            (10, 50),
            (50, 100),
            (100, 200),
            (200, 500),
            (500, 1000),
            (1000, 5000),
        ],
    )
    def test_step_monotonic_range(self, natr_low: int, natr_high: int) -> None:
        """In RANGE regime: natr_low < natr_high → step_low <= step_high."""
        step_low = compute_step_bps(natr_low, Regime.RANGE, DEFAULT_CONFIG)
        step_high = compute_step_bps(natr_high, Regime.RANGE, DEFAULT_CONFIG)
        assert step_low <= step_high, (
            f"Monotonicity violated: natr={natr_low}→step={step_low}, "
            f"natr={natr_high}→step={step_high}"
        )

    @pytest.mark.parametrize(
        "natr_low,natr_high",
        [
            (50, 100),
            (100, 500),
            (500, 2000),
        ],
    )
    def test_step_monotonic_vol_shock(self, natr_low: int, natr_high: int) -> None:
        """In VOL_SHOCK regime: monotonicity still holds."""
        step_low = compute_step_bps(natr_low, Regime.VOL_SHOCK, DEFAULT_CONFIG)
        step_high = compute_step_bps(natr_high, Regime.VOL_SHOCK, DEFAULT_CONFIG)
        assert step_low <= step_high

    def test_step_monotonic_via_evaluate(self) -> None:
        """Monotonicity holds through full evaluate() path."""
        policy = AdaptiveGridPolicy(DEFAULT_CONFIG)
        features_low = _make_features(natr_bps=50)
        features_high = _make_features(natr_bps=200)

        plan_low = policy.evaluate(features_low)
        plan_high = policy.evaluate(features_high)

        assert plan_low.spacing_bps <= plan_high.spacing_bps, (
            f"Monotonicity violated via evaluate(): "
            f"natr=50→spacing={plan_low.spacing_bps}, "
            f"natr=200→spacing={plan_high.spacing_bps}"
        )


# ===========================================================================
# B) Bounds
# ===========================================================================


class TestBounds:
    """Verify step_bps respects floor in all scenarios.

    INV-1: step_bps >= step_min_bps (always)
    """

    @pytest.mark.parametrize("natr_bps", [0, 1, 2, 5, 10, 50, 100, 500, 1000, 5000])
    def test_floor_holds(self, natr_bps: int) -> None:
        """step_bps >= step_min_bps for any natr_bps value."""
        step = compute_step_bps(natr_bps, Regime.RANGE, DEFAULT_CONFIG)
        assert step >= DEFAULT_CONFIG.step_min_bps, (
            f"Floor violated: step={step} < min={DEFAULT_CONFIG.step_min_bps} "
            f"at natr_bps={natr_bps}"
        )

    @pytest.mark.parametrize("regime", list(Regime))
    def test_floor_holds_all_regimes(self, regime: Regime) -> None:
        """Floor holds across all regime values."""
        step = compute_step_bps(100, regime, DEFAULT_CONFIG)
        assert step >= DEFAULT_CONFIG.step_min_bps

    def test_floor_holds_via_evaluate(self) -> None:
        """Floor holds through full evaluate() path."""
        policy = AdaptiveGridPolicy(DEFAULT_CONFIG)
        features = _make_features(natr_bps=1)  # Very low NATR
        plan = policy.evaluate(features)
        assert plan.spacing_bps >= DEFAULT_CONFIG.step_min_bps


# ===========================================================================
# C) Warmup / zero natr
# ===========================================================================


class TestWarmupZero:
    """Verify behavior when natr_bps is 0 (warmup or no data).

    When natr_bps=0, step_raw=0, so step_bps == step_min_bps.
    """

    def test_zero_natr_hits_floor(self) -> None:
        """natr_bps=0 → step_bps == step_min_bps."""
        step = compute_step_bps(0, Regime.RANGE, DEFAULT_CONFIG)
        assert step == DEFAULT_CONFIG.step_min_bps

    def test_zero_natr_all_regimes(self) -> None:
        """natr_bps=0 → step_bps == step_min_bps regardless of regime."""
        for regime in [Regime.RANGE, Regime.VOL_SHOCK, Regime.THIN_BOOK, Regime.TREND_UP]:
            step = compute_step_bps(0, regime, DEFAULT_CONFIG)
            assert step == DEFAULT_CONFIG.step_min_bps, (
                f"Expected floor at natr=0 for {regime}, got {step}"
            )

    def test_zero_natr_via_evaluate(self) -> None:
        """evaluate() with natr_bps=0 produces spacing_bps == step_min_bps."""
        policy = AdaptiveGridPolicy(DEFAULT_CONFIG)
        features = _make_features(natr_bps=0)
        plan = policy.evaluate(features)
        assert plan.spacing_bps == float(DEFAULT_CONFIG.step_min_bps)


# ===========================================================================
# D) Determinism
# ===========================================================================


class TestDeterminism:
    """Verify determinism: same inputs → identical output every time.

    Required for replay (ADR-001) and debugging.
    """

    def test_50_calls_compute_step(self) -> None:
        """50 repeated compute_step_bps calls produce identical results."""
        baseline = compute_step_bps(150, Regime.RANGE, DEFAULT_CONFIG)
        for i in range(50):
            result = compute_step_bps(150, Regime.RANGE, DEFAULT_CONFIG)
            assert result == baseline, f"Call {i} diverged: {result} != {baseline}"

    def test_50_calls_evaluate(self) -> None:
        """50 repeated evaluate() calls produce identical spacing_bps."""
        policy = AdaptiveGridPolicy(DEFAULT_CONFIG)
        features = _make_features(natr_bps=150)
        baseline = policy.evaluate(features)
        for i in range(50):
            result = policy.evaluate(features)
            assert result.spacing_bps == baseline.spacing_bps, (
                f"Call {i}: spacing {result.spacing_bps} != {baseline.spacing_bps}"
            )


# ===========================================================================
# E) Regime multiplier ordering
# ===========================================================================


class TestRegimeMultiplierOrdering:
    """Verify that regime multipliers produce expected step ordering.

    At fixed natr_bps, regimes with higher multipliers produce higher step.
    Default multipliers: RANGE=100, VOL_SHOCK=150, THIN_BOOK=200.
    """

    def test_vol_shock_gt_range(self) -> None:
        """VOL_SHOCK step >= RANGE step (mult 150 vs 100)."""
        step_range = compute_step_bps(100, Regime.RANGE, DEFAULT_CONFIG)
        step_vs = compute_step_bps(100, Regime.VOL_SHOCK, DEFAULT_CONFIG)
        assert step_vs >= step_range

    def test_thin_book_gt_vol_shock(self) -> None:
        """THIN_BOOK step >= VOL_SHOCK step (mult 200 vs 150)."""
        step_vs = compute_step_bps(100, Regime.VOL_SHOCK, DEFAULT_CONFIG)
        step_tb = compute_step_bps(100, Regime.THIN_BOOK, DEFAULT_CONFIG)
        assert step_tb >= step_vs

    def test_full_ordering(self) -> None:
        """Full ordering: RANGE <= VOL_SHOCK <= THIN_BOOK at natr=200."""
        step_r = compute_step_bps(200, Regime.RANGE, DEFAULT_CONFIG)
        step_v = compute_step_bps(200, Regime.VOL_SHOCK, DEFAULT_CONFIG)
        step_t = compute_step_bps(200, Regime.THIN_BOOK, DEFAULT_CONFIG)
        assert step_r <= step_v <= step_t, (
            f"Ordering violated: RANGE={step_r}, VOL_SHOCK={step_v}, THIN_BOOK={step_t}"
        )


# ===========================================================================
# F) Integer math contract
# ===========================================================================


class TestIntegerMath:
    """Verify all outputs are int (no float leakage).

    Required by ADR-022: "Integer arithmetic for all intermediate calculations."
    """

    @pytest.mark.parametrize("natr_bps", [0, 1, 50, 100, 333, 1000, 9999])
    def test_return_type_is_int(self, natr_bps: int) -> None:
        """compute_step_bps always returns int."""
        result = compute_step_bps(natr_bps, Regime.RANGE, DEFAULT_CONFIG)
        assert type(result) is int

    def test_spacing_bps_is_float_from_int(self) -> None:
        """GridPlan.spacing_bps is float (converted from int step)."""
        policy = AdaptiveGridPolicy(DEFAULT_CONFIG)
        features = _make_features(natr_bps=100)
        plan = policy.evaluate(features)
        assert isinstance(plan.spacing_bps, float)
        # Verify it's a whole number (originated from int)
        assert plan.spacing_bps == float(int(plan.spacing_bps))


# ===========================================================================
# G) Golden fixture
# ===========================================================================


class TestGoldenFixture:
    """Verify exact output for known inputs (regression anchor).

    Golden: natr_bps=100, RANGE regime, default config (alpha=30)
    Expected: (30 * 100 * 100) // 10000 = 30 bps
    """

    def test_golden_step(self) -> None:
        """natr_bps=100, RANGE, alpha=30 → step=30."""
        step = compute_step_bps(100, Regime.RANGE, DEFAULT_CONFIG)
        assert step == 30

    def test_golden_vol_shock(self) -> None:
        """natr_bps=100, VOL_SHOCK, alpha=30, mult=150 → step=45."""
        step = compute_step_bps(100, Regime.VOL_SHOCK, DEFAULT_CONFIG)
        assert step == 45

    def test_golden_thin_book(self) -> None:
        """natr_bps=100, THIN_BOOK, alpha=30, mult=200 → step=60."""
        step = compute_step_bps(100, Regime.THIN_BOOK, DEFAULT_CONFIG)
        assert step == 60

    def test_golden_floor(self) -> None:
        """natr_bps=10, RANGE, alpha=30 → raw=3 < min=5 → step=5."""
        step = compute_step_bps(10, Regime.RANGE, DEFAULT_CONFIG)
        assert step == 5  # Floor applied
