# NATR→Spacing Hook Contract (TRD-3b)

Normative specification of how `natr_bps` drives `spacing_bps` in the
adaptive grid policy. Contract tests: `tests/unit/test_adaptive_policy_natr_hook_contract.py`.

## Formula

`compute_step_bps()` in `src/grinder/policies/grid/adaptive.py`:

```
step_raw = (step_alpha * natr_bps * regime_mult) // 10000
step_bps = max(step_min_bps, step_raw)
```

Where:
- `natr_bps`: NATR(14) in x10000 integer bps (SSOT: `docs/23_NATR_CONTRACT.md`, ADR-078)
- `step_alpha`: scaled integer coefficient (default 30 = 0.30x)
- `regime_mult`: regime-dependent multiplier (scaled by 100)
- `step_min_bps`: floor (default 5 bps)

All arithmetic is integer (`//` division). No floating-point in the computation path.

## Regime multipliers

| Regime | `regime_mult` | Effective | Notes |
|--------|--------------|-----------|-------|
| RANGE | 100 | 1.00x | Base behavior |
| TREND_UP | 100 | 1.00x | Same as RANGE |
| TREND_DOWN | 100 | 1.00x | Same as RANGE |
| VOL_SHOCK | `vol_shock_step_mult` (150) | 1.50x | Wider step in volatile regime |
| THIN_BOOK | `thin_book_step_mult` (200) | 2.00x | Widest step in thin book |
| TOXIC | `thin_book_step_mult` (200) | 2.00x | Same as THIN_BOOK |

Ordering: RANGE <= VOL_SHOCK <= THIN_BOOK (contract-tested).

## Structural invariants

| ID | Invariant | Rationale |
|----|-----------|-----------|
| INV-1 | `step_bps >= step_min_bps` | Floor always holds |
| INV-2 | Monotonic in `natr_bps` | Higher vol → wider spacing (at fixed regime) |
| INV-3 | `step_bps == step_min_bps` when `natr_bps == 0` | Warmup fallback |
| INV-4 | Output is `int` | Integer math contract (ADR-022) |

## Determinism

Same `(natr_bps, regime, config)` → identical `int` step_bps on every call.
Achieved via integer-only arithmetic, no state, no randomness.

## What contract tests prove

- Monotonicity: parametrized across NATR range [10..5000] in RANGE and VOL_SHOCK
- Floor: holds for all NATR values [0..5000] and all Regime enum members
- Warmup: natr_bps=0 → step_min_bps across all regimes
- Determinism: 50 repeated calls (compute_step and evaluate)
- Regime ordering: RANGE <= VOL_SHOCK <= THIN_BOOK
- Integer math: return type is `int` for all inputs
- Golden fixture: exact values for known inputs (natr=100, alpha=30)

## What contract tests do NOT prove

- Width (X_stress) computation — covered by `test_adaptive_policy.py::TestComputeWidthBps`
- Levels computation — covered by `test_adaptive_policy.py::TestComputeLevels`
- Regime classification accuracy — covered by `test_regime.py`
- NATR(14) encoding — covered by `test_natr_contract.py` (TRD-3a)
- Auto-sizing integration — covered by `test_adaptive_policy.py::TestAutoSizingIntegration`
