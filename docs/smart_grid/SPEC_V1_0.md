# Adaptive Smart Grid v1.0
**Baseline adaptive grid spec**

### Status
- This document is a **versioned specification**. It must remain consistent with `docs/STATE.md`.
- Any contract/behavior changes require an ADR entry in `docs/DECISIONS.md` and determinism proofs.

---


## 17.1 Motivation

A “smart grid” is not a static symmetric lattice. In real markets:
- volatility regime changes,
- liquidity changes (thin book / impact spikes),
- execution quality changes (fills don’t happen instantly),
- trend days destroy “always-add” grids.

Therefore, a production-grade grid system must:
1) **Adapt** spacing/width/sizing to volatility and liquidity,
2) **Switch regimes** (range/trend/shock/toxic/thin),
3) **Enforce budgets** (DD caps, inventory caps, rate limits),
4) **Remain deterministic** in replay/paper and auditable via contracts and fixtures.

This spec describes how GRINDER implements Adaptive Smart Grid v1.

---

## 17.2 Goals

### Functional goals (v1)
- **Auto-parameterization:** compute grid parameters from features + budgets (no fixed magic numbers).
- **Regime-driven behavior:** explicit regimes with deterministic transitions.
- **Top-K 3–5 symbols:** select symbols designed for *tradable chop* + sufficient liquidity, not “highest vol”.
- **Protection:** throttle/pause/emergency behavior; adds-off in toxic/shock; reduce-only unloading.
- **Determinism:** same fixture + config → same digest.
- **SSOT honesty:** `STATE.md` clearly states what is implemented vs planned.

### Non-goals (v1)
- Guarantee “never realize a loss”.
- RL in production.
- Full exchange-matching-engine queue modeling.

---

## 17.3 Definitions

### Equity
Portfolio capital in quote currency (e.g., USDT).

### Notional
Position value in quote:
- `notional = qty * price`

### DD Budget
Allowed drawdown budget (policy input):
- portfolio DD budget: `dd_budget_portfolio`
- per-symbol budget: `dd_budget_symbol[s]`

### X_stress (Stress Move)
A dynamic estimate of adverse move magnitude on a chosen horizon H, used to set grid width/levels.

### Step (Spacing)
Grid spacing in percent/bps between levels.

### Width
Total coverage down/up, derived from X_stress (often symmetric in RANGE, asymmetric in TREND).

### Levels
Number of grid levels per side:
- `levels = ceil(width / step)` (clamped)

### Inventory & Skew
Inventory is current position exposure; skew is asymmetry to reduce inventory risk.

---

## 17.4 Data Inputs

### 17.4.1 L1 Snapshot (required)
Contract `Snapshot` must include at least:
- `bid_price`, `ask_price`, `bid_qty`, `ask_qty`
- `last_price` (optional), timestamps

L1 supports:
- `mid_price`, `spread_bps`
- L1 liquidity proxy (thin best level)
- L1 imbalance proxy

### 17.4.2 L2 OrderBook (optional but first-class)
To make L2 a real part of the system (not just docs), we support an optional orderbook on Snapshot:

- `Snapshot.book: Optional[OrderBook]`
- `OrderBook.bids: list[BookLevel]`, `asks: list[BookLevel]`
- `BookLevel.price`, `BookLevel.qty`

**Backwards compatibility:** fixtures without `book` remain valid (`book=None`).  
**Degradation:** if L2 is stale/unavailable → compute L2 features as `None` and fall back to L1 proxies.

### 17.4.3 Staleness
We define staleness thresholds for:
- L1 tick freshness
- L2 depth freshness (if used)

If stale:
- enter `THIN_BOOK` or `PAUSE` depending on severity, and/or disable L2 features.

---

## 17.5 Feature Pipeline

### 17.5.1 Bar construction (deterministic)
In replay/paper, features must be computed deterministically from fixture data.

We build OHLC bars from mid-price:
- `bar_interval_ms` fixed by config (e.g., 60_000 for 1m)
- `OHLC(mid)` from tick stream

### 17.5.2 Volatility (NATR / ATR)
Compute:
- `ATR(period)` from mid-bars
- `NATR = ATR / close`

**Truth note:** until real kline feed is integrated, this is **NATR(mid-bars)** and must be declared in `STATE.md`.

### 17.5.3 L1 microstructure features (v1)
- `mid_price`
- `spread_bps`
- `imbalance_l1 = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps)`
- `thin_l1 = min(bid_qty, ask_qty)` (optionally normalized to quote)

### 17.5.4 L2 microstructure features (v1.5+ but designed now)
If `book` present:
- `depth_topN_usd` (sum(qty*price) across topN bids/asks)
- `depth_imbalance_topN`
- `spread_at_depth(Q)` = effective spread for executing Q quote notional
- `impact_estimate_bps(Q)` = estimated price impact from walking topN
- `book_slope / liquidity_decay` (optional)
- `walls` detection (optional)

### 17.5.5 Range/trend indicators (no ML required in v1)
We compute:
- `sum_abs_returns` over horizon H
- `net_return = abs(p_t / p_{t-H} - 1)`
- `range_score = sum_abs_returns / (net_return + eps)`  (higher = more chop)
- `trend_strength` proxy (EMA slope / fast-slow divergence normalized)

---

## 17.6 Regime Model (State Machine)

### 17.6.1 Regimes
- `RANGE`: chop/mean reversion dominates
- `TREND_UP`, `TREND_DOWN`: directional move dominates
- `VOL_SHOCK`: volatility spikes
- `THIN_BOOK`: liquidity / depth deteriorates or L2 stale
- `TOXIC`: spreads/impact/jumps indicate hostile conditions
- `PAUSED`: manual or safety pause
- `EMERGENCY`: kill switch

### 17.6.2 Deterministic regime classification (v1)
Regime must be derived deterministically from features + gates:

Example precedence:
1) If `kill_switch_latched` → `EMERGENCY`
2) If `tox_severity >= HIGH` → `TOXIC`
3) Else if `thin_book = True` → `THIN_BOOK`
4) Else if `vol_shock = True` → `VOL_SHOCK`
5) Else if `trend_strength >= trend_th` → `TREND_*`
6) Else → `RANGE`

### 17.6.3 Regime actions
- RANGE → normal bilateral grid + mild skew
- TREND → skew + restrict adds + asym width
- VOL_SHOCK → widen step + adds off + reduce-only emphasis
- THIN_BOOK → throttle or pause + reduce-only if inventory exists
- TOXIC → pause new risk + reduce-only; emergency on extreme
- EMERGENCY → cancel orders + stop trading (latch)

---

## 17.7 Risk Budgeting (Auto)

### 17.7.1 Portfolio DD budget
Config provides a policy parameter:
- `dd_budget_pct` (or absolute)

System computes:
- `dd_budget_portfolio = equity * dd_budget_pct` (or provided absolute)

Budget window can be:
- daily, session-based, rolling (config)

### 17.7.2 Symbol budget allocator
Given selected Top-K symbols:
- **equal allocator:** `dd_i = dd_portfolio / K`
- **weighted allocator:** weights derived from liquidity and stability

Example weight:
- `w_i ∝ liquidity_score_i * (1 - tox_penalty_i)`
- normalize `w` → `dd_i = dd_portfolio * w_i`

Allocator must be deterministic.

### 17.7.3 Caps (hard limits)
Regardless of sizing:
- `max_effective_leverage` (portfolio)
- `max_inventory_notional` per symbol (may be % of equity)
- `max_open_orders` per symbol / portfolio
- order rate limits

---

## 17.8 Dynamic Stress Model (X_stress)

### 17.8.1 Inputs
- `NATR_tf` (from features)
- horizon `H_minutes` (depends on regime)
- tail multiplier `k_tail` (strictness)
- L2 penalty `l2_penalty >= 1` (impact/thinness)
- clamps `X_min`, `X_cap`

### 17.8.2 Computation
Let TF be the bar interval in minutes.  
`n = H / TF`

Approximate horizon volatility:
- `sigma_H ≈ NATR * sqrt(n)`

Stress width:
- `X_base = k_tail * sigma_H`
- `X_stress = clamp(X_base * l2_penalty, X_min, X_cap)`

Regime adjustments (deterministic):
- TREND: multiply by `trend_penalty` (or enforce asymmetric width)
- VOL_SHOCK/THIN/TOXIC: prefer shifting to protective modes rather than expanding width indefinitely

### 17.8.3 Width per side
- RANGE: typically symmetric (`width_up = width_down = X_stress`)
- TREND: asymmetric (increase width on the “against” side, reduce on “with” side)

---

## 17.9 Step Selection (Auto spacing)

### 17.9.1 Inputs
- `step_min_pct` (floor to avoid micro-grid)
- `alpha` volatility multiplier
- regime multiplier `shock_multiplier`

### 17.9.2 Computation
- `step_pct = max(step_min_pct, alpha * NATR * shock_multiplier(regime))`

Where:
- `shock_multiplier(RANGE)=1`
- `shock_multiplier(VOL_SHOCK)>1`
- `shock_multiplier(THIN_BOOK)` may be > `VOL_SHOCK`
- in TOXIC/EMERGENCY → step irrelevant (PAUSE)

---

## 17.10 Levels (Auto)

For each side:
- `levels = ceil(width / step)`
Clamp:
- `levels ∈ [levels_min, levels_max]`

**Rationale:** prevents pathological “hundreds of levels” at low vol and ensures bounded complexity.

---

## 17.11 Auto Sizing (Order sizes per level)

### 17.11.1 SSOT units
`GridPlan.size_schedule` MUST be interpreted as **quantity (base)** by Execution and Ledger.

If policy reasons in notional:
- `qty_i = notional_i / price_i`

### 17.11.2 Objective
Pick `size_schedule` such that:
- worst-case unrealized loss at `width_down` does not exceed `dd_budget_symbol`,
- inventory/leverage caps are respected,
- schedule is non-martingale (bounded growth).

### 17.11.3 Option A (v1 recommended): Equal notional per level
- Choose `notional_i = a` constant for all levels on the adverse side.
- Compute a deterministic loss coefficient `F(step, width, levels)`:
  - `worst_loss ≈ a * F(...)`
- Set:
  - `a = dd_budget_symbol / F(...)`
- Then:
  - `qty_i = a / price_i`

**Note:** `F` can be computed deterministically by summation using the chosen level prices.

### 17.11.4 Option B (v1.5): Tapered schedule (anti-fast-loading)
Define bounded weights `w_i` (monotonic but not exponential):
- constraints:
  - `max(w_i) / min(w_i) <= max_weight_ratio`
  - `sum(w_i)=1`
- Choose a scale `A` such that worst-case loss matches budget:
  - `notional_i = A * w_i`
- Convert to qty.

**Why:** reduces early over-loading and improves survivability in trend transitions.

### 17.11.5 Hard caps integration
After schedule computed:
- enforce `max_inventory_notional`
- enforce `max_effective_leverage`
- if violated:
  - reduce sizes, reduce levels, widen step, or switch regime to THROTTLE/PAUSE

This resolution order must be deterministic and documented.

---

## 17.12 Smart Grid Execution: Plan + Cycle behavior

### 17.12.1 Two-layer model
To support both existing plan-based execution and classical “grid cycles”, v1 uses two layers:

1) **GridPlan (policy output)** — desired grid geometry + mode + sizes.
2) **CycleEngine (execution helper)** — converts fills into TP orders and replenishment actions.

### 17.12.2 CycleEngine responsibilities (required for “grid cycles”)
On fill:
- BUY fill at `p_fill` with `qty`:
  - place SELL TP at `p_fill * (1 + step_pct)` for same `qty`
- SELL fill:
  - place BUY TP at `p_fill * (1 - step_pct)` for same `qty`

Replenishment:
- If `adds_allowed=True`:
  - place a new order further out to maintain level count
- If `adds_allowed=False`:
  - do not add new risk; only TP/reduce-only allowed

**Determinism:** order IDs, ordering, and replenishment selection must be stable.

### 17.12.3 ExecutionEngine responsibilities (existing)
- reconcile desired open orders vs actual open orders
- issue intents: PLACE/CANCEL/AMEND
- enforce port-level constraints (rate limits, order limits)

---

## 17.13 Paper/Replay Fill Model (must be realistic)

### 17.13.1 Deterministic crossing/touch fills (v1)
- LIMIT BUY fills if mid <= limit_price
- LIMIT SELL fills if mid >= limit_price
- one fill per order; no partial fills in v1

### 17.13.2 Slippage / partial fills (planned)
- L2-based impact model for taker-like execution
- partial fills when depth insufficient
- queue modeling is out-of-scope v1

> Until then, we must not claim RL readiness.

---

## 17.14 Protection & Gating

### 17.14.1 Toxicity Gate
Inputs can include:
- spread spikes (L1)
- impact estimate (L2)
- jump magnitude / volatility shock
- staleness

Outputs:
- severity: LOW/MID/HIGH/EXTREME
- action recommendation: NORMAL/THROTTLE/PAUSE/EMERGENCY
- reason codes

### 17.14.2 Vol Shock
When VOL_SHOCK:
- increase step via `shock_multiplier`
- set `adds_allowed=False` (at least temporarily)
- emphasize reduce-only unloading

### 17.14.3 Drawdown Guard → Damage Control
When `dd_utilization` exceeds thresholds:
- enter DAMAGE_CONTROL:
  - adds off
  - reduce-only
  - possibly widen TP distance or accelerate unloading
- if DD budget breached:
  - EMERGENCY (kill switch latch)

---

## 17.15 Top-K 3–5 Symbol Selection

### 17.15.1 Hard gates
Exclude symbols when:
- stale feed
- toxic HIGH+
- thin book severe
- spread too high
- optional: funding extreme (futures)

### 17.15.2 Scoring function (v1)
Aim: maximize *tradable chop with safe liquidity*.

Score example:
- `score = w_range*range_score + w_liq*liquidity_score - w_tox*tox_penalty - w_trend*trend_strength`

Liquidity score uses:
- L1 thin proxy and/or L2 depth/impact if available.

### 17.15.3 Selection
- select K in [3..5] (config)
- allocate budgets deterministically

---

## 17.16 ML Integration (Optional, honest)

### 17.16.1 ML is for:
- regime classification improvements
- execution-quality prediction (fill probability, impact risk)
- offline calibration of parameters (alpha, thresholds, weights)

### 17.16.2 ML is not for (v1):
- price prediction as a primary edge

### 17.16.3 Determinism requirement
ML inference in replay must:
- use versioned artifacts
- be deterministic (no randomness)
- be captured in acceptance/digest tests

---

## 17.17 RL (Planned only)
RL is only viable after:
- non-instant fills,
- partial fills/slippage,
- L2-aware execution environment.

Until then: RL must be considered **planned**, not implemented.

---

## 17.18 Observability

### 17.18.1 Required metrics
Performance:
- cycles_completed_total{symbol}
- realized_pnl_total{symbol}, unrealized_pnl{symbol}
- turnover_notional_total{symbol}
- fees_estimate_total{symbol}
- avg_cycle_profit_bps{symbol}

Risk:
- dd_current, dd_budget, dd_utilization_pct
- max_inventory_notional{symbol}
- effective_leverage
- time_in_regime_seconds{regime}
- adds_off_seconds{symbol}

Execution/Toxicity:
- spread_bps, impact_estimate_bps
- fill_rate, time_to_fill
- cancel_replace_rate
- tox_severity{symbol}

### 17.18.2 Alerts
- dd_utilization_pct > 0.8 (warning), > 1.0 (emergency)
- tox_severity HIGH sustained
- L2 stale sustained
- effective leverage over cap

---

## 17.19 Configuration Schema (policy-level; not fixed numbers)

A config controls *policy parameters*, not final grid parameters.  
Final parameters are computed from the market.

Key fields:
- K range: 3..5
- step_min_pct
- alpha
- horizon per regime
- k_tail per regime
- X_min / X_cap clamps
- levels min/max
- dd_budget policy and allocator
- caps (inventory, leverage, order count, rate)

---

## 17.20 Determinism & Testing

### 17.20.1 Determinism rules
- stable sorting
- stable order of symbols
- stable rounding/quantization (bps ints or fixed decimal quantization)
- no RNG
- model artifacts fixed by hash (if ML)

### 17.20.2 Required fixtures (minimum set)
- `range_day_l1` (chop)
- `trend_day_l1` (directional)
- `vol_shock_l1` (vol spike)
- `multisymbol_topk_l1` (Top-K scoring)
- `thin_book_l2` (if L2 added)
- `impact_spike_l2` (if L2 added)

### 17.20.3 DoD for “Implemented”
A feature is “implemented” only if:
- unit tests exist,
- fixtures cover it,
- determinism digest is updated and stable,
- `docs/STATE.md` is updated,
- ADR is added if contracts/behavior changed.

---

## 17.21 Implementation Roadmap (recommended PR sequence)

**P0**
1) Paper fills: instant → crossing/touch deterministic  
2) CycleEngine: fill → TP + replenish, adds-off integration  
3) SSOT units: qty vs notional clarified

**P1**
4) Feature engine: mid-bars OHLC + NATR + L1 microstructure  
5) L2 optional Snapshot.book + 2 L2 fixtures  
6) Regime controller v1 (heuristics) + AdaptiveGridPolicy (X_stress/step/levels/sizing)  
7) Top-K v1 (range+liq+tox scoring) + allocator

**P2**
8) ML artifacts + offline calibration (deterministic inference)  
9) RL only after partial fills/slippage (planned)

---

## 17.22 Appendix: Policy Pseudocode (high level)

1) compute features per symbol  
2) compute toxicity/thinness/staleness  
3) select Top-K (3–5) by score after hard gates  
4) allocate dd budgets per symbol  
5) for each selected symbol:
   - classify regime deterministically
   - compute X_stress from NATR/horizon/tails/L2 penalty
   - compute step from NATR/min step/shock multiplier
   - compute levels from width/step
   - compute size schedule from dd budget and loss coefficient
   - enforce caps; if violated → adjust or throttle/pause
   - output GridPlan with reason codes
6) execution reconciles desired orders, CycleEngine handles fill→TP cycles

