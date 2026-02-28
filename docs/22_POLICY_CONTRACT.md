# Policy Interface Contract (TRD-2)

Normative specification of the GridPolicy + GridPlan interface.
Contract tests: `tests/unit/test_policy_contract.py`.
ADR: ADR-077 in `docs/DECISIONS.md`.

## GridPolicy ABC

`src/grinder/policies/base.py` — the abstract base class for all grid policies.

| Member | Signature | Returns |
|--------|-----------|---------|
| `name` | `str` class attribute | Policy identifier |
| `evaluate` | `(self, features: dict[str, Any]) -> GridPlan` | Grid plan for current tick |
| `should_activate` | `(self, features: dict[str, Any]) -> bool` | Whether policy is applicable |

### Concrete implementations

| Policy | Module | `name` | Notes |
|--------|--------|--------|-------|
| `StaticGridPolicy` | `policies/grid/static.py` | `STATIC_GRID` | Fixed params, always BILATERAL/RANGE |
| `AdaptiveGridPolicy` | `policies/grid/adaptive.py` | `ADAPTIVE_GRID` | Regime-aware, deterministic integer math |

`AdaptiveGridPolicy.evaluate()` accepts additional keyword arguments
(`kill_switch_active`, `toxicity_result`, `l2_features`, `dd_budget_ratio`)
beyond the base `features` dict.  These are **not** part of the ABC contract —
they are extension points specific to the adaptive implementation.

## GridPlan dataclass

`src/grinder/policies/base.py` — the output of every `evaluate()` call.

### Field enumeration (11 fields)

| # | Field | Type | Default | Required | Description |
|---|-------|------|---------|----------|-------------|
| 1 | `mode` | `GridMode` | — | yes | Grid operation mode (BILATERAL, UNI_LONG, etc.) |
| 2 | `center_price` | `Decimal` | — | yes | Quote currency per base unit |
| 3 | `spacing_bps` | `float` | — | yes | Grid step in basis points |
| 4 | `levels_up` | `int` | — | yes | Levels above center (sell/short) |
| 5 | `levels_down` | `int` | — | yes | Levels below center (buy/long) |
| 6 | `size_schedule` | `list[Decimal]` | — | yes | Base asset quantity per level |
| 7 | `skew_bps` | `float` | `0.0` | no | Inventory skew offset |
| 8 | `regime` | `MarketRegime` | `RANGE` | no | Detected market regime |
| 9 | `width_bps` | `float` | `0.0` | no | Computed grid width |
| 10 | `reset_action` | `ResetAction` | `NONE` | no | Auto-reset action |
| 11 | `reason_codes` | `list[str]` | `[]` | no | Diagnostic reason codes |

Adding or removing a field changes the policy-engine contract.
Contract tests will fail if the field set changes.

### Structural invariants

Every valid `GridPlan` must satisfy:

| ID | Invariant | Rationale |
|----|-----------|-----------|
| INV-1 | `spacing_bps > 0` | Grid step must be positive |
| INV-2 | `levels_up >= 0` and `levels_down >= 0` | Non-negative; 0 = paused grid |
| INV-3 | `center_price > 0` | Must be a valid price |
| INV-4 | If `levels_up > 0` or `levels_down > 0`, then `len(size_schedule) > 0` | Active grid needs sizing |
| INV-5 | All entries in `size_schedule >= 0` | No negative quantities |
| INV-6 | `reason_codes` is `list[str]` | Type contract for downstream consumers |

## Determinism contract

Both `StaticGridPolicy` and `AdaptiveGridPolicy` are deterministic:
same `features` dict produces an identical `GridPlan` on every call.
This is required for replay (ADR-001) and debugging.

`AdaptiveGridPolicy` achieves determinism via integer-scaled arithmetic
(all config values stored as scaled integers, e.g., `step_alpha=30` = 0.30).

## What contract tests prove

- Protocol: both implementations are `isinstance(GridPolicy)` with correct signatures
- Fields: `GridPlan` has exactly 11 named fields with expected types
- Invariants: every `evaluate()` output passes INV-1 through INV-6
- Determinism: 10 repeated evaluations produce `==` results
- Static behavior: always `mode=BILATERAL`, `regime=RANGE`, `reset_action=NONE`

## What contract tests do NOT prove

- Correctness of adaptive math (step/width/level formulas) — covered by `tests/unit/test_adaptive_grid.py`
- Regime classification accuracy — covered by `tests/unit/test_regime.py`
- Runtime integration with the engine gate chain — covered by `tests/unit/test_safety_envelope.py`
- L2 gating or DD budget ratio behavior — covered by adaptive-specific tests

The contract tests complement — but do not replace — implementation-specific tests.
