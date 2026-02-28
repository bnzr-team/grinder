"""GridPolicy + GridPlan normative contract tests (TRD-2).

Lock down the policy interface and GridPlan structure as regression contracts:
- Protocol conformance: evaluate() → GridPlan, should_activate() → bool
- Field enumeration: GridPlan has exactly 11 fields (frozen set)
- Invariants: spacing_bps > 0, levels >= 0, center_price > 0, size_schedule not empty
- Determinism: same input → identical output (static + adaptive)

These are CONTRACT tests — if they break, a policy interface changed.
See docs/22_POLICY_CONTRACT.md for the normative specification.
See ADR-077 in docs/DECISIONS.md for the design decision.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from grinder.core import GridMode, MarketRegime, ResetAction
from grinder.policies.base import GridPlan, GridPolicy
from grinder.policies.grid.adaptive import AdaptiveGridConfig, AdaptiveGridPolicy
from grinder.policies.grid.static import StaticGridPolicy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GRIDPLAN_EXPECTED_FIELDS = frozenset(
    {
        "mode",
        "center_price",
        "spacing_bps",
        "levels_up",
        "levels_down",
        "size_schedule",
        "skew_bps",
        "regime",
        "width_bps",
        "reset_action",
        "reason_codes",
    }
)


@pytest.fixture()
def static_policy() -> StaticGridPolicy:
    return StaticGridPolicy(
        spacing_bps=10.0,
        levels=5,
        size_per_level=Decimal("0.01"),
    )


@pytest.fixture()
def adaptive_policy() -> AdaptiveGridPolicy:
    return AdaptiveGridPolicy(AdaptiveGridConfig())


@pytest.fixture()
def basic_features() -> dict[str, Any]:
    """Minimal features dict sufficient for StaticGridPolicy."""
    return {"mid_price": Decimal("50000")}


@pytest.fixture()
def adaptive_features() -> dict[str, Any]:
    """Features dict sufficient for AdaptiveGridPolicy."""
    return {
        "ts": 1_000_000,
        "symbol": "BTCUSDT",
        "mid_price": Decimal("50000"),
        "natr_bps": 30,
        "spread_bps": 5,
        "thin_l1": Decimal("1.0"),
        "net_return_bps": 0,
        "range_score": 50,
        "warmup_bars": 20,
    }


# ===========================================================================
# A) Protocol conformance
# ===========================================================================


class TestProtocolConformance:
    """Verify that concrete policies conform to the GridPolicy ABC.

    SSOT formula (docs/22_POLICY_CONTRACT.md):
        Every GridPolicy subclass must:
        - have a `name` str attribute
        - implement evaluate(features) -> GridPlan
        - implement should_activate(features) -> bool
    """

    def test_static_is_grid_policy(self, static_policy: StaticGridPolicy) -> None:
        """StaticGridPolicy is a concrete GridPolicy subclass."""
        assert isinstance(static_policy, GridPolicy)

    def test_adaptive_is_grid_policy(self, adaptive_policy: AdaptiveGridPolicy) -> None:
        """AdaptiveGridPolicy is a concrete GridPolicy subclass."""
        assert isinstance(adaptive_policy, GridPolicy)

    def test_static_evaluate_returns_grid_plan(
        self,
        static_policy: StaticGridPolicy,
        basic_features: dict[str, Any],
    ) -> None:
        """StaticGridPolicy.evaluate() returns a GridPlan instance."""
        result = static_policy.evaluate(basic_features)
        assert isinstance(result, GridPlan)

    def test_adaptive_evaluate_returns_grid_plan(
        self,
        adaptive_policy: AdaptiveGridPolicy,
        adaptive_features: dict[str, Any],
    ) -> None:
        """AdaptiveGridPolicy.evaluate() returns a GridPlan instance."""
        result = adaptive_policy.evaluate(adaptive_features)
        assert isinstance(result, GridPlan)

    def test_static_should_activate_returns_bool(
        self,
        static_policy: StaticGridPolicy,
        basic_features: dict[str, Any],
    ) -> None:
        """StaticGridPolicy.should_activate() returns bool."""
        result = static_policy.should_activate(basic_features)
        assert isinstance(result, bool)

    def test_adaptive_should_activate_returns_bool(
        self,
        adaptive_policy: AdaptiveGridPolicy,
        adaptive_features: dict[str, Any],
    ) -> None:
        """AdaptiveGridPolicy.should_activate() returns bool."""
        result = adaptive_policy.should_activate(adaptive_features)
        assert isinstance(result, bool)

    def test_static_has_name(self, static_policy: StaticGridPolicy) -> None:
        """StaticGridPolicy has a name attribute."""
        assert static_policy.name == "STATIC_GRID"

    def test_adaptive_has_name(self, adaptive_policy: AdaptiveGridPolicy) -> None:
        """AdaptiveGridPolicy has a name attribute."""
        assert adaptive_policy.name == "ADAPTIVE_GRID"


# ===========================================================================
# B) Field enumeration (GridPlan has exactly 11 fields)
# ===========================================================================


class TestFieldEnumeration:
    """Verify GridPlan field set is frozen at exactly 11 fields.

    Adding or removing a field changes the policy-engine contract.
    If this test breaks, update docs/22_POLICY_CONTRACT.md and ADR-077.
    """

    def test_gridplan_has_exactly_11_fields(self) -> None:
        """GridPlan dataclass has exactly 11 fields — no more, no less."""
        actual_fields = frozenset(f.name for f in dataclasses.fields(GridPlan))
        assert actual_fields == GRIDPLAN_EXPECTED_FIELDS, (
            f"GridPlan field mismatch.\n"
            f"  Expected: {sorted(GRIDPLAN_EXPECTED_FIELDS)}\n"
            f"  Actual:   {sorted(actual_fields)}\n"
            f"  Added:    {sorted(actual_fields - GRIDPLAN_EXPECTED_FIELDS)}\n"
            f"  Removed:  {sorted(GRIDPLAN_EXPECTED_FIELDS - actual_fields)}"
        )

    def test_gridplan_field_types(self) -> None:
        """GridPlan field types match the normative contract.

        base.py does not use `from __future__ import annotations`,
        so dataclass field types are resolved class objects, not strings.
        """
        field_map = {f.name: f.type for f in dataclasses.fields(GridPlan)}

        # Required fields (no defaults)
        assert field_map["mode"] is GridMode
        assert field_map["center_price"] is Decimal
        assert field_map["spacing_bps"] is float
        assert field_map["levels_up"] is int
        assert field_map["levels_down"] is int
        assert field_map["size_schedule"] == list[Decimal]

        # Optional fields (with defaults)
        assert field_map["skew_bps"] is float
        assert field_map["regime"] is MarketRegime
        assert field_map["width_bps"] is float
        assert field_map["reset_action"] is ResetAction
        assert field_map["reason_codes"] == list[str]


# ===========================================================================
# C) GridPlan invariants
# ===========================================================================


class TestGridPlanInvariants:
    """Verify that all policy outputs satisfy structural invariants.

    These invariants hold for EVERY valid GridPlan, regardless of policy.
    """

    def test_static_plan_invariants(
        self,
        static_policy: StaticGridPolicy,
        basic_features: dict[str, Any],
    ) -> None:
        """StaticGridPolicy output satisfies all GridPlan invariants."""
        plan = static_policy.evaluate(basic_features)
        _assert_plan_invariants(plan)

    def test_adaptive_plan_invariants(
        self,
        adaptive_policy: AdaptiveGridPolicy,
        adaptive_features: dict[str, Any],
    ) -> None:
        """AdaptiveGridPolicy output satisfies all GridPlan invariants."""
        plan = adaptive_policy.evaluate(adaptive_features)
        _assert_plan_invariants(plan)

    def test_static_plan_mode_bilateral(
        self,
        static_policy: StaticGridPolicy,
        basic_features: dict[str, Any],
    ) -> None:
        """StaticGridPolicy always returns BILATERAL mode, RANGE regime, NONE reset."""
        plan = static_policy.evaluate(basic_features)
        assert plan.mode == GridMode.BILATERAL
        assert plan.regime == MarketRegime.RANGE
        assert plan.reset_action == ResetAction.NONE

    def test_adaptive_plan_valid_enums(
        self,
        adaptive_policy: AdaptiveGridPolicy,
        adaptive_features: dict[str, Any],
    ) -> None:
        """AdaptiveGridPolicy output uses valid enum values."""
        plan = adaptive_policy.evaluate(adaptive_features)
        assert isinstance(plan.mode, GridMode)
        assert isinstance(plan.regime, MarketRegime)
        assert isinstance(plan.reset_action, ResetAction)


# ===========================================================================
# D) Determinism
# ===========================================================================


class TestDeterminism:
    """Verify that same inputs produce identical outputs (contract: no randomness).

    Determinism is required for replay (ADR-001) and debugging.
    """

    def test_static_determinism(
        self,
        static_policy: StaticGridPolicy,
        basic_features: dict[str, Any],
    ) -> None:
        """StaticGridPolicy: 10 evaluations produce identical GridPlans."""
        baseline = static_policy.evaluate(basic_features)
        for _ in range(10):
            result = static_policy.evaluate(basic_features)
            assert result == baseline

    def test_adaptive_determinism(
        self,
        adaptive_policy: AdaptiveGridPolicy,
        adaptive_features: dict[str, Any],
    ) -> None:
        """AdaptiveGridPolicy: 10 evaluations produce identical GridPlans."""
        baseline = adaptive_policy.evaluate(adaptive_features)
        for _ in range(10):
            result = adaptive_policy.evaluate(adaptive_features)
            assert result == baseline


# ===========================================================================
# Helpers
# ===========================================================================


def _assert_plan_invariants(plan: GridPlan) -> None:
    """Assert structural invariants that every valid GridPlan must satisfy."""
    # INV-1: spacing_bps > 0 (grid step must be positive)
    assert plan.spacing_bps > 0, f"spacing_bps must be positive, got {plan.spacing_bps}"

    # INV-2: levels >= 0 (non-negative; 0 = paused grid)
    assert plan.levels_up >= 0, f"levels_up must be >= 0, got {plan.levels_up}"
    assert plan.levels_down >= 0, f"levels_down must be >= 0, got {plan.levels_down}"

    # INV-3: center_price > 0
    assert plan.center_price > 0, f"center_price must be positive, got {plan.center_price}"

    # INV-4: if grid has levels, size_schedule must not be empty
    if plan.levels_up > 0 or plan.levels_down > 0:
        assert len(plan.size_schedule) > 0, "size_schedule must not be empty when grid has levels"

    # INV-5: all sizes must be non-negative
    for i, size in enumerate(plan.size_schedule):
        assert size >= 0, f"size_schedule[{i}] must be >= 0, got {size}"

    # INV-6: reason_codes is a list of strings
    assert isinstance(plan.reason_codes, list)
    for code in plan.reason_codes:
        assert isinstance(code, str), f"reason_code must be str, got {type(code)}"
