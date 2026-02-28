# NATR(14) Volatility Feature Contract (TRD-3a)

Normative specification of the `natr_bps` feature — the single source of truth
for normalized ATR volatility in the Grinder feature pipeline.
Contract tests: `tests/unit/test_natr_contract.py`.
ADR: ADR-078 in `docs/DECISIONS.md`.

## Feature identity

| Property | Value |
|----------|-------|
| Field name | `natr_bps` |
| Location | `FeatureSnapshot.natr_bps` (`src/grinder/features/types.py`) |
| Computation | `compute_natr_bps()` (`src/grinder/features/indicators.py`) |
| Type | `int` |
| Encoding | `int((ATR / close * 10000).quantize(1))` — integer basis points |
| Period | 14 (configured via `FeatureEngineConfig.atr_period`) |
| Warmup | Requires `period + 1` bars (15 for ATR(14)) |
| Default | `0` during warmup |

## Encoding contract

`natr_bps` uses **x10000 integer basis-point encoding**:

```
natr_bps = int((ATR(14) / close * 10000).quantize(Decimal("1")))
```

Examples:
| ATR | Close | NATR ratio | natr_bps |
|-----|-------|------------|----------|
| 10 | 100 | 0.10 | 1000 |
| 50 | 100 | 0.50 | 5000 |
| 1 | 100 | 0.01 | 100 |
| 5 | 50000 | 0.0001 | 1 |

**No dual encoding**: `natr_bps` (x10000) is the only NATR representation
in the feature pipeline. Consumers needing different scales (x1000, float ratio)
must derive from `natr_bps` at the call site. This avoids drift between
redundant fields (see ADR-078).

## Structural invariants

Every call to `compute_natr_bps(bars, period)` must satisfy:

| ID | Invariant | Rationale |
|----|-----------|-----------|
| INV-1 | `natr_bps >= 0` | Volatility is non-negative |
| INV-2 | `natr_bps == 0` when `len(bars) < period + 1` | Warmup guard |
| INV-3 | `natr_bps == 0` when `bars[-1].close == 0` | Division-by-zero guard |

## Determinism contract

`compute_natr_bps` is deterministic: same `bars` list and `period` produce
an identical `int` result on every call. This is required for replay (ADR-001)
and debugging.

Determinism is achieved via:
- `Decimal` arithmetic throughout (no floating-point rounding variance)
- Integer quantization at the output boundary (`quantize(Decimal("1"))`)
- No internal state or randomness

## FeatureSnapshot integration

`FeatureSnapshot.natr_bps` is populated by `FeatureEngine.process_snapshot()`:
1. Bar builder produces completed `MidBar` objects from tick stream
2. `compute_natr_bps(bars, config.atr_period)` converts to integer bps
3. Result stored in `FeatureSnapshot.natr_bps`
4. Exposed via `to_policy_features()["natr_bps"]` for policy consumption

Serialization roundtrip: `FeatureSnapshot.to_dict()["natr_bps"]` is `int`,
`FeatureSnapshot.from_dict()` restores it as `int`.

## What contract tests prove

- Encoding: golden fixture (15 bars, TR=10, close=100) → exactly 1000 bps
- Return type: always `int` (not float, not Decimal)
- Formula match: `compute_natr_bps` == manual `int(ATR/close * 10000)`
- Invariants: INV-1 (non-negative), INV-2 (warmup guard), INV-3 (zero-close guard)
- Warmup boundary: 14 bars → 0, 15 bars → non-zero
- Determinism: 100 repeated calls → identical result
- FeatureSnapshot: field type is `int`, roundtrip preserves value, policy features include it

## What contract tests do NOT prove

- ATR algorithm correctness (SMA vs EMA, windowing) — covered by `tests/unit/test_indicators.py::TestATR`
- FeatureEngine bar construction from tick stream — covered by `tests/unit/test_feature_engine.py`
- AdaptiveGridPolicy consumption of `natr_bps` for step/width calculations — covered by `tests/unit/test_adaptive_grid.py`
- L2 features or other volatility indicators — out of scope

The contract tests complement — but do not replace — implementation-specific tests.
