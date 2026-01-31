# GRINDER - Adaptive Grid Controller Spec

> SSOT for: **market regime selection**, **adaptive grid step**, and **auto-reset**.
>
> This spec defines **target behavior**. Unless `docs/STATE.md` says implemented, treat as *Planned*.

---

## 16.1 Status

- **Status:** Proposed (docs-only)
- **Affects:**
  - `docs/05_FEATURE_CATALOG.md` (regime inputs, wall persistence)
  - `docs/07_GRID_POLICY_LIBRARY.md` (GridPlan contract extensions)
  - `docs/08_STATE_MACHINE.md` (reset transitions + throttle/pause semantics)
  - `docs/10_RISK_SPEC.md` (risk overrides + churn limits)
  - `docs/11_BACKTEST_PROTOCOL.md` (baseline comparisons + execution-quality metrics)
  - `docs/13_OBSERVABILITY.md` (regime/step/reset metrics + reason codes)
  - `docs/15_CONSTANTS.md` (controller thresholds)
  - `docs/STATE.md` and `docs/DECISIONS.md` (truth + ADR)

---

## 16.2 Motivation

Static grid parameters degrade when the market regime shifts (range → trend, thin book, volatility shock).
Research/practice indicates best ROI comes from a **meta-controller** that:

1) selects **market regime**
2) adapts **grid parameters** (step/width/levels/size/skew)
3) triggers **auto-reset** on regime switch / drift / execution-quality deterioration

In GRINDER this controller must be:

- **Deterministic** (replayable; stable digest)
- **Explainable** (reason-codes; no silent behavior changes)
- **Observable** (Prometheus metrics + structured logs)

---

## 16.3 Definitions

- **Regime** — discrete market state used to choose grid mode and parameterization.
- **Step** (`spacing_bps`) — distance between grid levels in bps.
- **Width** (`width_bps`) — effective grid range in bps (policy/plan-owned).
- **Reset** — plan change requiring cancellations and re-quoting (SOFT/HARD).
- **Gating** — conditions under which trading must throttle/pause.

---

## 16.4 Contracts (SSOT)

### 16.4.1 Enums

```python
from enum import Enum

class MarketRegime(Enum):
    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    VOL_SHOCK = "VOL_SHOCK"
    THIN_BOOK = "THIN_BOOK"
    TOXIC = "TOXIC"
    PAUSED = "PAUSED"  # service/forced state

class ResetAction(Enum):
    NONE = "NONE"
    SOFT = "SOFT"
    HARD = "HARD"
```

### 16.4.2 GridPlan contract extensions

`GridPlan` MUST include these fields (see `docs/07_GRID_POLICY_LIBRARY.md`):

- `regime: MarketRegime`
- `mode: GridMode`
- `center: float`
- `spacing_bps: float`
- `width_bps: float`
- `levels_long: int`
- `levels_short: int`
- `size_per_level: float`
- `skew_bps: float`
- `reset_action: ResetAction`
- `reason_codes: list[str]` (stable dictionary)

**Rule:** every tick that emits a plan must include at least one `reason_code`.

---

## 16.5 Decision pipeline (per tick)

Order is fixed and deterministic:

1) compute features
2) compute gating signals (tox/spread/depth/wall persistence)
3) infer `MarketRegime`
4) compute target grid parameters (step/width/levels/size/skew)
5) decide `ResetAction`
6) emit `GridPlan + reason_codes`
7) execution applies the plan

---

## 16.6 Regime selection (MVP rule-based)

### 16.6.1 Inputs

Required derived inputs (see `docs/05_FEATURE_CATALOG.md`):

- `tox_score`
- `spread_bps`
- `natr_14_5m`
- `trend_slope_5m`
- `price_jump_bps_1m`
- `depth_top5_usd`
- optional: `wall_persistence_score` (anti-spoof proxy)

### 16.6.2 Priority logic

1) if `tox_score >= TOX_PAUSE` → `TOXIC` (or `PAUSED` if risk forces)
2) if `spread_bps >= SPREAD_PAUSE_BPS` OR `depth_top5_usd <= DEPTH_MIN_USD` → `THIN_BOOK`
3) if `abs(price_jump_bps_1m) >= SHOCK_BPS` → `VOL_SHOCK`
4) if `abs(trend_slope_5m) >= TREND_SLOPE_TH` → `TREND_UP / TREND_DOWN`
5) else → `RANGE`

### 16.6.3 Hysteresis (anti-chatter)

Mandatory constants:

- `REGIME_MIN_HOLD_TICKS`
- `REGIME_SWITCH_COOLDOWN_TICKS`

Rules:

- regime cannot flip more often than hold/cooldown
- **override allowed** only for `TOXIC`, `VOL_SHOCK`, or risk emergency

---

## 16.7 Adaptive step (spacing_bps)

Goal: expand step when vol/spread increase and contract in calm range.

### 16.7.1 MVP formula (deterministic)

Definitions:

- `step_raw = max(STEP_BASE_BPS, natr_14_5m * STEP_VOL_MULT, spread_bps * STEP_SPREAD_MULT)`
- `step_clamped = clamp(step_raw, STEP_MIN_BPS, STEP_MAX_BPS)`
- `spacing_bps = EMA(prev_spacing_bps, step_clamped, alpha=STEP_EMA_ALPHA)`

State:

- EMA uses controller state; must be replayed deterministically (no wall-clock).

### 16.7.2 Width/levels coupling

SSOT choice:

- `levels_*` are regime-specific defaults (policy-owned).
- `width_bps` must be computed explicitly from `spacing_bps` and levels (policy-owned),
  e.g. `width_bps = spacing_bps * (levels_long + levels_short) / 2`.

---

## 16.8 Auto-reset

Reset is first-class behavior; it must be observable and testable.

### 16.8.1 Triggers

A) Regime switch:

- if regime changed (post-hysteresis) → `SOFT` or `HARD` (see table below)

B) Center drift:

- if `abs(mid - center)/mid*10000 >= CENTER_DRIFT_BPS` → `SOFT` or `HARD`

C) Step drift:

- if `abs(spacing_bps - current_spacing_bps) >= STEP_RESET_DELTA_BPS` → `SOFT`

D) Execution-quality deterioration (optional in MVP but contractable):

- persistent drop in fill-rate + rising adverse selection proxy → `SOFT` / `PAUSE`

### 16.8.2 Default regime-pair reset table

- RANGE ↔ TREND_* : HARD
- ANY → TOXIC or VOL_SHOCK : HARD + THROTTLE/PAUSE
- RANGE ↔ THIN_BOOK : SOFT
- TREND_UP ↔ TREND_DOWN : HARD

### 16.8.3 Meaning of SOFT vs HARD

- SOFT: cancel only non-conforming/outside orders; minimize churn
- HARD: cancel all orders; rebuild full ladder from plan

---

## 16.9 Reason codes (stable dictionary)

`reason_codes` must include at least one code per tick.
If reset/gating occurs, related reason code must be present.

Initial dictionary:

- `GATE_TOXIC`
- `GATE_THIN_BOOK`
- `GATE_SPREAD`
- `REGIME_RANGE`
- `REGIME_TREND_UP`
- `REGIME_TREND_DOWN`
- `REGIME_VOL_SHOCK`
- `RESET_REGIME_SWITCH`
- `RESET_CENTER_DRIFT`
- `RESET_STEP_DRIFT`
- `RESET_EXEC_QUALITY`
- `STEP_VOL_ADJ`
- `STEP_SPREAD_FLOOR`
- `STEP_SMOOTHING`

---

## 16.10 Observability (contract)

Metrics additions (see `docs/13_OBSERVABILITY.md`):

- `grinder_regime{symbol,regime}`
- `grinder_grid_step_bps{symbol}`
- `grinder_grid_width_bps{symbol}`
- `grinder_reset_total{symbol,type,reason}`
- `grinder_gate_state{symbol,state}`
- `grinder_reason_code_total{code}`

Structured logs must include:

- `regime`, `spacing_bps`, `width_bps`, `reset_action`, `reason_codes`.

---

## 16.11 Backtest requirements (when code lands)

Any PR that changes regime/step/reset/policy behavior MUST:

- compare against **Baseline A: Static Grid** (no regime, no adaptive step, no reset)
- report execution-quality metrics:
  - fill-rate, cancel/replace rate (churn), adverse selection proxy

Determinism:

- identical fixture + config must yield identical digest (`verify_replay_determinism`)

---

## 16.12 Definition of Done (implementation)

Implementation is considered complete only when:

- `GridPlan` emits regime/step/width/reset/reason_codes
- replay digest is stable across runs
- unit tests cover regime hysteresis, step bounds, reset triggers
- observability metrics/log fields exist
- `docs/STATE.md` updated to mark the controller as implemented (not planned)
