# 26 - Rolling Infinite Grid Specification

**Status:** draft
**Date:** 2026-03-09
**ADR:** ADR-085
**Supersedes:** portions of doc-25 SS 25.5 Step 2 (symmetric bilateral grid centered on mid_price)

---

## 1. Problem Statement

LiveGridPlannerV1 (doc-25) builds a **symmetric bilateral grid centered on `mid_price`**.
Each tick, the planner recomputes all desired levels from the current mid. When price moves:

1. All levels shift: full `GRID_SHIFT` (10 CANCEL + 10 PLACE = 20 actions for N=5).
2. Budget burns fast: Run #18 exhausted 100-order budget in ~2 minutes (5 shifts x 20 actions).
3. Fills are lost: price moves, grid recenters, filled level's neighbors vanish before exploitation.
4. Anti-churn and converge-first (PR-P0-RACE-1) mitigate but don't solve the root cause.

**Root cause:** the grid is anchored to a moving target (`mid_price`), so every price
movement triggers a full rebuild. Grid fills don't advance the grid -- they get swallowed
by the next recenter.

### Desired behavior

A **rolling infinite ladder** where:

- Grid fills advance the ladder in the fill direction.
- The sell-side frontier follows the buy-side down (and vice versa).
- Level count and spacing are preserved.
- Each fill triggers minimal actions (1 CANCEL + 2 PLACE = 3), not a full rebuild.

---

## 2. Design Goals

| ID | Goal |
|----|------|
| G1 | Grid fills shift the ladder discretely, not recenter on mid |
| G2 | Uniform spacing maintained at all times |
| G3 | Constant level count (`N_buy + N_sell`) absent risk/budget/exchange blocks |
| G4 | Minimal actions per fill: O(1), not O(N) |
| G5 | TP fills restore grid depth without shifting ladder |
| G6 | Deterministic restart recovery |
| G7 | Backward compatible (existing env vars, safe-by-default via feature flag) |

---

## 3. Formal Invariants

### INV-1: Cardinality

At any point, the desired ladder has exactly `N_buy` BUY levels and `N_sell` SELL levels,
unless blocked by risk gate, budget exhaustion, or exchange rejection.

### INV-2: Spacing

For any side, adjacent desired levels differ by exactly `step_price` (computed once per
session from the anchor). Formally:

```
|price[i+1] - price[i]| == step_price    for all adjacent i on same side
```

### INV-3: Rolling Shift

After `k` net grid BUY fills (without intervening grid SELL fills):

- `net_offset = -k`
- Effective center shifted down by `k * step_price`
- Entire desired ladder shifted down by `k * step_price`

Symmetrically, `k` net grid SELL fills produce `net_offset = +k`, ladder shifted up.

### INV-4: Frontier Replenish

New orders are placed at the **frontier** of the appropriate side (farthest from center),
not via mid-price recenter. The filled level's slot is consumed; a new slot opens at the
opposite frontier.

### INV-5: No Global Rebuild on Fill

An ordinary grid fill generates at most `1 CANCEL + 2 PLACE = 3 actions`.
Full `GRID_SHIFT` (cancel-all + place-all) is NEVER triggered by a fill event alone.

### INV-6: TP Isolation

TP fills (`strategy_id="tp"`) do NOT shift the ladder (`net_offset` unchanged).
After TP fill, the planner restores missing grid levels via normal `GRID_FILL`
(planner diff). No separate replenish mechanism needed.

### INV-7: Bidirectional Symmetry

BUY fill path and SELL fill path are exact mirrors. Every rule that applies to
"BUY fill shifts down" has an equivalent "SELL fill shifts up".

### INV-8: Restart Recovery

After restart or AccountSync reconnect:

1. `ladder_anchor = current mid_price`
2. `net_offset = 0`
3. `step_price = round_to_tick(ladder_anchor * spacing_bps / 10000)`
4. Planner diffs desired grid against exchange orders.
5. Orphaned orders cancelled via `GRID_TRIM`; missing levels placed via `GRID_FILL`.
6. Rolling state begins fresh from new anchor.

---

## 4. State Model

### 4.1 Rolling Ladder State (per symbol, in-memory, volatile)

```python
@dataclass
class _RollingLadderState:
    anchor_price: Decimal     # Mid price when grid was first built this session
    step_price: Decimal       # round_to_tick(anchor_price * spacing_bps / 10000)
    net_offset: int = 0       # +1 per grid SELL fill, -1 per grid BUY fill
```

**Lifecycle:**

- Created on first planner tick (when grid is first built).
- Updated on each grid fill event (`net_offset` only).
- Destroyed on restart (volatile, not persisted).

### 4.2 Effective Center

```
effective_center = anchor_price + net_offset * step_price
```

This is the center around which desired levels are computed. It moves discretely
by `+/- step_price` on each grid fill, NOT continuously with `mid_price`.

### 4.3 SSOT for Ladder State

The `_RollingLadderState` is the **sole source of truth** for desired level computation.
The exchange (open orders via AccountSync) is the source of truth for actual state.
The planner diffs desired vs actual.

`mid_price` is NOT used for level computation after initialization. It is still used for:

- Initial anchor computation (first tick).
- Feature engine / NATR calculation.
- Adaptive spacing computation (if enabled).
- Display / logging.

---

## 5. Level Computation Formulas

### 5.1 Desired Levels

```
BUY[i]  = round_to_tick(effective_center - i * step_price),   i = 1..N_buy
SELL[i] = round_to_tick(effective_center + i * step_price),    i = 1..N_sell
```

Where:

- `effective_center = anchor_price + net_offset * step_price`
- `round_to_tick(p) = floor(p / tick_size) * tick_size`

### 5.2 Step Price Computation

`step_price` is computed ONCE when the ladder is initialized:

```
step_price = round_to_tick(anchor_price * effective_spacing_bps / 10000)
```

Where `effective_spacing_bps` is:

- **Static:** `base_spacing_bps` (config)
- **Adaptive:** `max(natr_bps * step_alpha / 100, step_min_bps)` (if NATR enabled and fresh)

`step_price` does NOT change after initialization, even if NATR changes. This
preserves INV-2 (uniform spacing). Step refresh happens only on re-anchor events
(restart, or future manual re-anchor).

### 5.3 Numeric Example

BTCUSDT, `anchor_price = 66000`, `spacing_bps = 10`, `tick_size = 0.10`, N=3:

```
step_price = round_to_tick(66000 * 10 / 10000) = round_to_tick(66.0) = 66.0
```

Initial grid (`net_offset = 0`, `effective_center = 66000`):

```
SELL: 66066.0 (i=1)  66132.0 (i=2)  66198.0 (i=3)
BUY:  65934.0 (i=1)  65868.0 (i=2)  65802.0 (i=3)
```

After 1 BUY fill at 65934 (`net_offset = -1`, `effective_center = 65934`):

```
SELL: 66000.0 (i=1)  66066.0 (i=2)  66132.0 (i=3)
BUY:  65868.0 (i=1)  65802.0 (i=2)  65736.0 (i=3)
```

---

## 6. Action Generation Rules

### 6.1 Grid BUY Fill

**Trigger:** Grid order (`strategy_id="d"`, side=BUY) disappears from exchange
(detected via AccountSync snapshot diff, not in `_pending_cancels`).

**State update:**

```
net_offset -= 1     # effective_center moves down by step_price
```

**Planner diff (next tick):**

Using the example from 5.3 (center was 66000, now 65934 after BUY fill at 65934):

| Desired | Exchange | Result |
|---------|----------|--------|
| SELL 66132 | SELL 66132 | MATCH |
| SELL 66066 | SELL 66066 | MATCH |
| SELL 66000 | -- | MISSING: PLACE |
| -- | SELL 66198 | EXTRA: CANCEL |
| BUY 65868 | BUY 65868 | MATCH |
| BUY 65802 | BUY 65802 | MATCH |
| BUY 65736 | -- | MISSING: PLACE |

**Actions:** 1 CANCEL + 2 PLACE = 3 actions.

### 6.2 Grid SELL Fill

**Mirror of 6.1:**

```
net_offset += 1     # effective_center moves up by step_price
```

**Actions:** CANCEL lowest BUY + PLACE new SELL at top frontier + PLACE new BUY at
inner frontier. 3 actions total.

### 6.3 Multiple Grid Fills (Same Snapshot)

If `k` grid BUY fills detected in one snapshot:

```
net_offset -= k
```

**Actions:** `k` CANCEL + `2k` PLACE = `3k` actions. Linear in fill count.

For N=5, 2 fills: 6 actions (vs 20 for full rebuild). For N=5, 1 fill: 3 actions (vs 20).

### 6.4 TP Fill

**Trigger:** TP order (`strategy_id="tp"`) disappears from exchange.

**State update:** NONE. `net_offset` unchanged.

**Planner diff:** TP fill itself doesn't remove a grid order. However, if
`TP_SLOT_TAKEOVER` previously cancelled a grid order to make room for the TP,
that grid level is now missing.

**Actions:** 0-1 PLACE (`GRID_FILL` to restore the slot-takeover'd level). The
planner handles this naturally via diff -- no special `TP_FILL_REPLENISH` logic needed.

### 6.5 Mid-Price Drift (No Fills)

When `mid_price` moves but no fills occur:

- `net_offset` unchanged.
- `effective_center` unchanged.
- Desired levels unchanged.
- Planner diff: all match (within epsilon).
- **Actions: 0.**

This is the key behavioral change from doc-25: mid-price drift does NOT trigger
`GRID_SHIFT`. The grid is anchored to rolling ladder state, not to `mid_price`.

### 6.6 When Does GRID_SHIFT Occur?

Full grid rebuild (`GRID_SHIFT`) occurs ONLY on:

1. **Restart / re-anchor:** new anchor, new desired levels, diff against exchange.
2. **Explicit re-anchor:** future manual command or extended flat-period reset.
3. **Emergency:** if exchange state diverges beyond recovery threshold.

`GRID_SHIFT` is NEVER triggered by:

- Normal price movement.
- Grid fills.
- TP fills.

---

## 7. Tick Pipeline Order

### 7.1 Current Order (doc-25)

```
1. AccountSync tick -> snapshot
2. Grid freeze check
3. Planner (computes desired from mid_price)
4. Anti-churn filter
5. Convergence guards
6. Cycle layer (fill detection -> TP generation -> replenish)
```

Fill detection runs AFTER the planner. With rolling grid, fills must update
`net_offset` BEFORE the planner computes desired levels.

### 7.2 Required Order (rolling grid)

```
1. AccountSync tick -> snapshot
2. Grid freeze check (modified: see 8.2)
3. Fill detection (extract from cycle layer, update net_offset)
4. Planner (computes desired from effective_center)
5. Convergence guards
6. Cycle layer remainder (TP generation, TP expiry, TP renew)
```

**Key change:** fill detection (currently Phase 1 of `LiveCycleLayerV1.on_snapshot()`)
must execute before the planner. This means either:

- (a) Extract fill detection from `on_snapshot()` into a separate method called earlier.
- (b) Add a pre-planner fill scan that only updates `net_offset` (cycle layer still runs
  later for TP generation).

Decision deferred to implementation PR.

---

## 8. Interaction with Existing Mechanisms

### 8.1 Converge-First (PR-P0-RACE-1, ADR-084)

**Status: PRESERVED, unchanged.**

Rolling shifts still dispatch PLACEs and CANCELs. Convergence guards still apply:

- Guard 1 (inflight latch): wait for AccountSync after dispatch.
- Guard 2 (cancel-first on extras): if stale orders exist, cancel only.
- Guard 3 (budget pre-check): if budget insufficient, defer.

Key improvement: rolling shifts use 3 actions per fill (vs 20 for full rebuild),
so budget burns ~7x slower.

### 8.2 Grid Freeze in Position (PR-FREEZE)

**Status: MUST BE MODIFIED.**

Current behavior: freeze ALL planner output when position open.

Problem: grid BUY fill opens a position. Freeze blocks the planner. The 3-action
rolling shift (CANCEL top SELL + PLACE inner SELL + PLACE frontier BUY) never
executes. On TP fill (position closes), grid unfreezes with stale desired levels.

**Required change:** fill-driven shifts MUST be exempt from grid freeze. The freeze
should prevent mid-price-driven rebuilds (which don't exist in rolling mode anyway)
but not fill-driven ladder shifts.

Implementation options (to decide in implementation PR):

- (a) Disable grid freeze entirely when rolling mode is active.
- (b) Keep freeze but exempt fill-driven shifts (flag: `net_offset` changed since last plan).
- (c) Apply freeze only after the fill-driven shift completes (shift first, then freeze).

### 8.3 Anti-Churn (PR-ANTI-CHURN)

**Status: LARGELY OBSOLETE for rolling mode.**

Anti-churn suppresses mid-price-driven `GRID_SHIFT` when move < 50bps. Rolling grid
doesn't produce mid-driven shifts at all. Anti-churn still applies to:

- Restart re-anchor (one-time shift).
- Explicit re-anchor events.

Fill-driven shifts bypass anti-churn (they are by definition desired behavior).

### 8.4 TP Atomicity (PR-P0-TP-CLOSE-ATOMIC, ADR-083)

**Status: PRESERVED, unchanged.**

TP PLACE + TP_SLOT_TAKEOVER CANCEL remain atomic via `correlation_id`. Rolling grid
doesn't change TP generation -- it's in the cycle layer, not the planner.

### 8.5 TP Slot Takeover (PR-ROLL-2)

**Status: PRESERVED, cooperates with rolling shift.**

When TP created after grid fill:

1. Cycle layer cancels farthest same-side grid order (`TP_SLOT_TAKEOVER`).
2. Rolling shift already wants to CANCEL that same order (it's "extra" after shift).
3. `TP_SLOT_TAKEOVER` pre-cancels the extra; planner sees fewer extras on next tick.

Net effect: `TP_SLOT_TAKEOVER` and rolling shift cooperate. No conflict, no double-cancel
(planner skips already-cancelled orders).

### 8.6 Replenish (PR-INV-4) and TP_FILL_REPLENISH (engine)

**Status: OBSOLETE in rolling mode.**

Current replenish: after grid fill, place order at `level+1` (further from center). This
was needed because the current planner recenters on mid, losing filled level context.

Rolling grid: after grid fill, planner already computes the correct shifted desired
levels. Missing levels are restored via normal `GRID_FILL` from planner diff. No
separate replenish mechanism needed.

After TP fill: the slot-takeover'd grid order is restored by planner diff (`GRID_FILL`).
No separate `TP_FILL_REPLENISH` needed.

**Migration:** replenish mechanisms should be disabled when rolling mode is active.
Formal removal deferred to cleanup PR.

### 8.7 Reduce-Only Enforcement (PR-ROLL-1)

**Status: PRESERVED, unchanged.**

Rolling grid PLACEs are still classified by intent (`INCREASE_RISK` / `REDUCE_RISK`).
Reduce-only enforcement applies to PLACEs on the opposite side when position is open.

Note: the inner SELL placed after a BUY fill (see 6.1) is `INCREASE_RISK` -- it opens
a potential short entry. This is correct: the grid is designed to capture both sides.
Risk is managed by Gate 5 (max position cap), not by blocking grid PLACEs.

---

## 9. Order Matching Strategy

### 9.1 Price-Based Matching (Change from doc-25)

**Current** (doc-25 SS 25.5 Step 3): Match exchange orders to desired levels by
`level_id` in `clientOrderId`.

**Problem:** After a rolling shift, exchange orders retain their original `level_id`
from a previous center. A SELL originally at `level_id=1` (from old center) might
now correspond to `level_id=2` (from new center). Level-id matching produces
false mismatches, triggering unnecessary CANCEL+PLACE.

**Rolling grid:** Match by **(side, price)** within `price_epsilon` tolerance.

### 9.2 Matching Algorithm

```
For each desired level (side, price):
    Find unmatched exchange order with same side and
        |exchange_price - desired_price| <= price_epsilon
    If found: MATCH (no action)
    If not found: MISSING -> PLACE

For each unmatched exchange order (after all desired matched):
    EXTRA -> CANCEL
```

### 9.3 Level ID in New Orders

New PLACEs use the level index from the desired grid (`i` in `BUY[i]` / `SELL[i]`).
This is for identification/logging purposes only -- matching does NOT depend on
`level_id`.

---

## 10. Restart / AccountSync Recovery

### 10.1 On Engine Start

1. No persisted rolling state -- ladder is volatile (in-memory).
2. First AccountSync snapshot provides `mid_price` and `open_orders`.
3. Engine initializes:
   ```
   anchor_price = mid_price
   step_price   = round_to_tick(anchor_price * effective_spacing_bps / 10000)
   net_offset   = 0
   ```
4. Planner computes desired levels from anchor.
5. Diff against exchange: CANCEL extras (`GRID_TRIM`), PLACE missing (`GRID_FILL`).

### 10.2 State Loss on Restart

After restart, `net_offset` is lost. The grid re-anchors at current `mid_price`.

**Consequence:** Some orders may be cancelled and replaced at slightly different
prices (old center != new anchor). This is a one-time cost bounded by
`N_buy + N_sell` actions.

**Acceptable because:**

- Restart is infrequent.
- One-time rebuild is bounded.
- Alternative (persisted state) adds complexity and failure modes.
- The exchange-truth reconciliation (INV-8) handles all cases cleanly.

### 10.3 AccountSync Reconnect (Without Restart)

If AccountSync loses connection and reconnects (no engine restart):

- Rolling state preserved (in-memory, engine still running).
- Next snapshot shows current exchange orders.
- Planner diffs as usual.
- No re-anchor needed.

---

## 11. Grid Fill vs TP Fill Distinction

### 11.1 SSOT

`is_tp_order(client_order_id)` at `src/grinder/reconcile/identity.py:174`.

- Grid order: `strategy_id == "d"` (grid identity).
- TP order: `strategy_id == "tp"` (TP identity).

### 11.2 Effect on Ladder

| Event | `net_offset` | Ladder shift | Planner actions |
|-------|-------------|--------------|-----------------|
| Grid BUY fill | `-1` | Down by `step_price` | 1 CANCEL + 2 PLACE |
| Grid SELL fill | `+1` | Up by `step_price` | 1 CANCEL + 2 PLACE |
| TP fill | unchanged | None | 0-1 PLACE (restore slot) |

---

## 12. What Becomes Obsolete in Rolling Mode

| Mechanism | Status | Rationale |
|-----------|--------|-----------|
| GRID_SHIFT from mid drift | Eliminated | Grid doesn't track mid_price |
| Anti-churn (mid-price suppression) | Largely N/A | No mid-driven shifts to suppress |
| Grid freeze (planner suppression) | Must modify | Fill-driven shifts must pass |
| Replenish (cycle_layer) | Obsolete | Planner diff handles level restoration |
| TP_FILL_REPLENISH (engine) | Obsolete | Planner diff handles slot restoration |
| `_grid_anchor_mid` | Replaced | Rolling state replaces mid-anchor |
| Hysteresis (`rebalance_threshold`) | Reduced scope | No continuous mid tracking |

---

## 13. Test Matrix (for Implementation PR)

### Invariant Tests

| ID | Test | Invariant |
|----|------|-----------|
| T1 | After 1 BUY fill: desired has N_buy BUY + N_sell SELL levels | INV-1 |
| T2 | After 1 SELL fill: N_buy + N_sell preserved | INV-1 |
| T3 | Adjacent desired levels differ by exactly step_price | INV-2 |
| T4 | After 1 BUY fill: effective_center decreased by step_price | INV-3 |
| T5 | After k BUY fills: effective_center decreased by k * step_price | INV-3 |
| T6 | After 1 SELL fill: effective_center increased by step_price | INV-3 |
| T7 | New orders at frontier, not at center | INV-4 |
| T8 | Fill generates exactly 1 CANCEL + 2 PLACE = 3 actions | INV-5 |
| T9 | TP fill does NOT change net_offset | INV-6 |
| T10 | BUY fill path mirrors SELL fill path | INV-7 |
| T11 | After restart: anchor=mid, offset=0, grid rebuilt | INV-8 |

### Integration Tests

| ID | Test | Verifies |
|----|------|----------|
| T12 | k fills in one snapshot: 3k actions | Multi-fill scaling |
| T13 | TP_SLOT_TAKEOVER reduces planner extras count | Cooperation |
| T14 | Convergence guards apply to fill-driven shifts | Guard compat |
| T15 | Budget check: 3 PLACEs per fill fit budget | Budget compat |
| T16 | Price-based matching: correct price, wrong level_id -> MATCH | New matching |
| T17 | Mid-price drift produces 0 planner actions | No mid-shift |
| T18 | TP fill restores slot-takeover'd level via GRID_FILL | TP restoration |

### Backward Compatibility

| ID | Test | Verifies |
|----|------|----------|
| T19 | `GRINDER_LIVE_ROLLING_GRID=0` (default): current behavior preserved | Feature flag |
| T20 | Rolling mode off: GRID_SHIFT still triggered by mid drift | Legacy compat |

---

## 14. Env Vars

| Var | Type | Default | Purpose |
|-----|------|---------|---------|
| `GRINDER_LIVE_ROLLING_GRID` | bool | `False` | Enable rolling infinite grid mode |

Safe-by-default: disabled. Current mid-anchored behavior (doc-25) is the default.
Requires `GRINDER_LIVE_PLANNER_ENABLED=1` to take effect.

---

## 15. Migration Path

### Phase 1: Spec PR (this PR)

- Define contract, invariants, formulas, interaction map.
- No code changes.

### Phase 2: Implementation PR

- New matching strategy (price-based).
- Rolling state tracking (`net_offset`).
- Fill detection before planner (tick pipeline reorder).
- Feature flag: `GRINDER_LIVE_ROLLING_GRID`.
- Disable replenish in rolling mode.
- Modify grid freeze for fill-driven shifts.
- Tests T1-T20.

### Phase 3: Live Verification PR

- Live trading run with rolling grid enabled.
- Forensic pack comparison: rolling vs mid-anchored.
- Budget usage, fill count, grid stability metrics.

### Phase 4: Cleanup PR (optional)

- Remove obsolete replenish mechanisms.
- Simplify anti-churn for rolling mode.
- Remove grid freeze override for rolling mode.

---

## 16. Open Questions (to resolve before Phase 2)

1. **Adaptive spacing refresh:** When NATR changes significantly, should `step_price`
   be updated? Current spec says no (fixed per session). Extended sessions may
   benefit from periodic refresh with re-anchor.

2. **Max drift from market:** If `net_offset` grows large, the grid drifts far from
   current market. Should there be a `max_offset` threshold that triggers re-anchor?

3. **Grid freeze interaction:** Should fill-driven shifts be exempt from freeze (option b),
   or should freeze be disabled entirely for rolling mode (option a)?

4. **Replenish removal timing:** Should replenish be disabled immediately when rolling
   mode is active, or kept as a fallback during Phase 2 development?

5. **Fill detection timing:** Should fill detection be extracted from cycle layer into a
   separate pre-planner phase, or should `net_offset` be computed independently?
