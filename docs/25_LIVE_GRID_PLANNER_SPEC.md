# LiveGridPlanner Specification (doc-25)

Normative specification for the live grid reconciliation engine.
Replaces PaperEngine as the source of order decisions in live/mainnet mode.

## 25.1 Purpose

PaperEngine tracks orders in an internal `ExecutionState` that is never
synchronized with the exchange. In live mode this causes:

- **No cancel/replace:** paper state shows all orders "open" even after
  exchange fills/expires them, so reconciliation sees no diff.
- **Ghost orders:** internal state diverges from exchange reality.

LiveGridPlanner solves this by using **exchange open orders (via AccountSync)
as source of truth**, diffing against a computed desired grid, and emitting
`PLACE / CANCEL / REPLACE` actions.

PaperEngine remains the simulation/backtest engine (unchanged).

## 25.2 Inputs

| Input | Source | Type | Notes |
|-------|--------|------|-------|
| `mid_price` | Snapshot (WS) | `Decimal` | Current mid-price |
| `spread_bps` | Snapshot (WS) | `float` | Current spread |
| `ts_ms` | Snapshot (WS) | `int` | Epoch milliseconds |
| `symbol` | Config | `str` | Trading pair |
| `open_orders` | `AccountSnapshot` (AccountSync) | `tuple[OpenOrderSnap, ...]` | Exchange truth |
| `positions` | `AccountSnapshot` (AccountSync) | `tuple[PositionSnap, ...]` | For notional checks |
| `natr_bps` | FeatureEngine | `int \| None` | NATR(14) in integer bps; `None` = not available |
| `natr_last_ts` | FeatureEngine | `int` | Timestamp of last NATR computation |
| `regime` | `classify_regime()` | `MarketRegime` | From `controller/regime.py`; `RANGE` if unavailable |
| `config` | `LiveGridConfig` | dataclass | Grid parameters (see 25.3) |

## 25.3 LiveGridConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_spacing_bps` | `float` | `10.0` | Static grid spacing (used as-is or as adaptive base) |
| `levels` | `int` | `5` | Levels per side (total = 2 * levels) |
| `size_per_level` | `Decimal` | `Decimal("0.01")` | Order quantity per level (base asset) |
| `adaptive_enabled` | `bool` | `False` | Use NATR-driven spacing |
| `step_alpha` | `int` | `30` | NATR multiplier (scaled /100) |
| `step_min_bps` | `int` | `5` | Spacing floor |
| `natr_stale_ms` | `int` | `120_000` | NATR staleness threshold (2 * bar_interval) |
| `rebalance_threshold_steps` | `float` | `1.0` | Min grid shift (in steps) to trigger rebalance |
| `price_epsilon_bps` | `float` | `0.5` | Price match tolerance for diff |
| `qty_epsilon_pct` | `float` | `1.0` | Qty match tolerance (%) |
| `cooldown_ms` | `int` | `100` | Min interval between action batches |

## 25.4 Output

```python
@dataclass
class GridPlanResult:
    actions: list[ExecutionAction]  # PLACE / CANCEL / REPLACE
    desired_count: int              # len(desired grid levels)
    actual_count: int               # len(matched exchange orders)
    diff_missing: int               # desired but not on exchange
    diff_extra: int                 # on exchange but not in desired
    diff_mismatch: int              # matched but price/qty differs
    effective_spacing_bps: float    # actual spacing used
    natr_fallback: bool             # True if NATR unavailable, using static
```

## 25.5 Core Algorithm

### Step 1: Compute effective spacing

```
if adaptive_enabled and natr_bps is not None and natr_age_ms <= natr_stale_ms:
    effective_spacing_bps = compute_step_bps(natr_bps, regime, config)
else:
    effective_spacing_bps = base_spacing_bps
    natr_fallback = True
```

Uses existing `compute_step_bps()` from `policies/grid/adaptive.py` (TRD-3b).
`regime` comes from `classify_regime()` in `controller/regime.py` — uses
FeatureEngine outputs (natr_bps, spread_bps, l1_depth). If regime classification
is unavailable (e.g., FeatureEngine warmup), defaults to `RANGE` (1.0x multiplier).
Fallback emits gauge `grinder_grid_natr_fallback_active = 1`.

### Step 2: Build desired grid

Symmetric bilateral grid centered on `mid_price`:

```
for i in 1..levels:
    buy_price  = mid_price * (1 - i * effective_spacing_bps / 10000)
    sell_price = mid_price * (1 + i * effective_spacing_bps / 10000)
```

Each level has a canonical key: `{side}:L{i}` (e.g., `BUY:L1`, `SELL:L3`).

Prices are rounded to `tick_size` (from symbol constraints). If `tick_size`
is unavailable for the symbol, planner emits zero actions (fail-safe) and
logs `WARN "No tick_size for {symbol}, skipping grid plan"`. This prevents
placing orders at invalid price increments (Binance -4014).

### Step 3: Match exchange orders to desired levels

For each exchange order in `open_orders`:

1. Parse `clientOrderId` using existing `parse_client_order_id()` (identity.py).
2. Extract `level_id` from parsed ID.
3. Build key `{side}:L{level_id}`.
4. If key matches a desired level, it's a **match**. Check price/qty tolerance.
5. If no match, it's **extra** (candidate for CANCEL).

For each desired level with no matching exchange order: **missing** (candidate for PLACE).

### Step 4: Apply rebalance hysteresis

Count how many grid steps the center has shifted since last plan:

```
shift_steps = abs(mid_price - last_plan_center) / (mid_price * effective_spacing_bps / 10000)
```

If `shift_steps < rebalance_threshold_steps` AND no missing/extra orders:
  skip rebalance (return empty actions). This prevents churn from micro-movements.

If there are missing orders (fills/expires removed them), always rebalance
regardless of shift threshold — the grid has gaps that need filling.

### Step 5: Generate actions

| Condition | Action | Reason tag |
|-----------|--------|------------|
| Desired level missing from exchange | `PLACE` | `GRID_FILL` |
| Exchange order not in desired grid | `CANCEL` | `GRID_TRIM` |
| Matched but price differs > epsilon | `CANCEL` + `PLACE` (or `REPLACE`) | `GRID_SHIFT` |
| Matched but qty differs > epsilon | `CANCEL` + `PLACE` (or `REPLACE`) | `GRID_RESIZE` |

`REPLACE` is used only if the port supports atomic replace (currently
`BinanceFuturesPort.replace_order` does cancel+place internally, so CANCEL+PLACE
is equivalent). The planner emits CANCEL+PLACE for clarity; the port can optimize.

## 25.6 Invariants

| ID | Invariant | Rationale |
|----|-----------|-----------|
| I1 | Exchange is source of truth for open orders | No ghost state |
| I2 | `effective_spacing_bps >= step_min_bps` always | Floor holds even in fallback |
| I3 | No actions if shift < threshold AND no missing/extra | Anti-churn |
| I4 | NATR fallback is observable (gauge + log) | Operator visibility |
| I5 | `clientOrderId` scheme used for level matching | Deterministic diff |
| I6 | Orders not matching our `clientOrderId` prefix are ignored | Don't touch non-grinder orders |
| I7 | Read-only: planner never calls exchange directly | Actions go through LiveEngine pipeline |
| I8 | Planner is stateless per call (except last_plan_center cache) | Testable, deterministic |

## 25.7 NATR Fallback Policy

When `natr_bps` is unavailable or stale:

- **Condition:** `natr_bps is None` OR `(ts_ms - natr_last_ts) > natr_stale_ms`
- **Behavior:** use `base_spacing_bps` as static fallback
- **Observable:** `grinder_grid_natr_fallback_active` gauge = 1
- **Log:** `WARN "NATR fallback active: using base_spacing_bps={base_spacing_bps}"`
- **Recovery:** automatic when fresh NATR arrives (gauge resets to 0)

Stale threshold default: `natr_stale_ms = 120_000` (2 * 60s bar interval).

## 25.8 Interaction with Existing Systems

### FSM Orchestrator

Planner behavior depends on FSM state:

- **INIT/READY**: Planner is skipped entirely (FSM defer, PR-338).
- **ACTIVE**: Normal mode — planner emits PLACE + CANCEL as computed.
- **PAUSED/DEGRADED/THROTTLED/EMERGENCY**: Planner runs in **cancel-only mode**
  (`suppress_increase=True`, PR-INV-2). Only CANCEL actions are emitted;
  PLACE/REPLACE are filtered out. This prevents risk accumulation while
  still allowing order cleanup/trimming.

### Suppress Increase Mode (PR-INV-2)

`plan(..., suppress_increase=True)` filters the action list after generation:

- CANCEL actions: **kept** (trim/cleanup always allowed)
- PLACE / REPLACE actions: **removed** (no new risk)

Enabled automatically by LiveEngineV0 when `FSM state != ACTIVE`.
No side effects — only the returned action list is affected.

### Gate Chain

Actions from planner flow through the existing gate chain in LiveEngineV0:
fill-prob gate, SOR, notional limits, armed check, etc. The planner does not
bypass any safety gate.

### AccountSync

Planner consumes `AccountSnapshot.open_orders` from the last successful sync.
If sync failed (error), planner uses the last known good snapshot. If no
snapshot has ever succeeded, planner emits zero actions (safe startup).

### PaperEngine

In live mode, PaperEngine is **not used for order decisions**. It may still
run in shadow mode for backtesting/comparison. The switch is controlled by
a flag in LiveEngineV0 (e.g., `GRINDER_LIVE_PLANNER_ENABLED`).

### Kill Switch / Emergency Exit

Kill switch triggers FSM PAUSED, emergency exit triggers EMERGENCY.
Both cause FSM != ACTIVE, so planner runs in cancel-only mode (PR-INV-2)
and existing emergency exit logic in LiveEngineV0 handles cleanup.

## 25.9 Observability

### Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_grid_plan_actions_total` | counter | `op={place\|cancel\|replace}`, `reason` | Actions generated per plan cycle |
| `grinder_grid_desired_orders` | gauge | `symbol` | Number of desired grid levels |
| `grinder_grid_actual_orders` | gauge | `symbol` | Number of matched exchange orders |
| `grinder_grid_diff_orders` | gauge | `symbol`, `kind={missing\|extra\|mismatch}` | Diff breakdown |
| `grinder_grid_effective_spacing_bps` | gauge | `symbol` | Current effective spacing |
| `grinder_grid_natr_fallback_active` | gauge | — | 1 if using static fallback |
| `grinder_grid_last_plan_ts` | gauge | — | Timestamp of last plan execution |
| `grinder_grid_rebalance_skipped_total` | counter | — | Skipped due to hysteresis |

### Alerts (proposed)

| Alert | Expr | For | Severity | Description |
|-------|------|-----|----------|-------------|
| `GridPlannerStale` | `grinder_grid_last_plan_ts > 0 and (time() * 1000 - grinder_grid_last_plan_ts) > 60000` | 3m | warning | Planner hasn't run in 60s |
| `GridNatrFallbackProlonged` | `grinder_grid_natr_fallback_active == 1` | 10m | warning | NATR unavailable for 10+ min |

### Log format

```
INFO  grid_plan: symbol=ETHUSDT desired=10 actual=8 missing=2 extra=0 mismatch=0
      actions=[PLACE BUY:L4 @ 2195.30 qty=0.02, PLACE BUY:L5 @ 2188.72 qty=0.02]
      spacing_bps=3.0 natr_fallback=false shift_steps=1.3
```

## 25.10 FeatureEngine in LiveEngineV0

Currently `FeatureEngine` lives inside PaperEngine. For the live planner to
access `natr_bps`, FeatureEngine must be lifted into LiveEngineV0:

- LiveEngineV0 instantiates `FeatureEngine` with same config as PaperEngine
- On each snapshot, feeds `FeatureEngine.process_snapshot(snapshot)`
- Reads `natr_bps` from the latest `L1FeatureSnapshot`
- PaperEngine retains its own FeatureEngine instance for backtest (no change)

This is a **wiring change only** — FeatureEngine API and internals are unchanged.

## 25.11 Implementation Plan

| PR | Scope | Files | Depends on |
|----|-------|-------|------------|
| PR-L0 | Lift FeatureEngine into LiveEngineV0 | `live/engine.py` | — |
| PR-L1 | LiveGridPlannerV1 + tests | `live/grid_planner.py` (new), `tests/unit/test_live_grid_planner.py` (new) | PR-L0 |
| PR-L2 | Wire planner into LiveEngineV0 | `live/engine.py`, `scripts/run_trading.py` | PR-L1 |
| PR-L3 | Metrics + alerts + runbook | `monitoring/alert_rules.yml`, `docs/runbooks/` | PR-L2 |

## 25.12 Acceptance Criteria

### AC1: Price shift triggers rebalance

Given: 10 open orders placed at mid_price=2200, spacing=3 bps (~$0.66)
When: mid_price moves to 2202 (shift > 1 step)
Then: planner emits CANCEL for old levels + PLACE for new levels

### AC2: Fill/expire triggers replenishment

Given: 10 desired levels, exchange shows 8 orders (2 filled/expired)
When: planner runs
Then: 2x PLACE actions for missing levels

### AC3: No churn below threshold

Given: mid_price moves by 0.3 steps (below threshold=1.0)
When: planner runs AND no missing/extra orders
Then: zero actions emitted, `rebalance_skipped_total` incremented

### AC4: NATR fallback

Given: `natr_bps = None` or stale (age > 120s)
When: planner runs
Then: uses `base_spacing_bps`, `natr_fallback_active=1`

### AC5: Non-grinder orders ignored

Given: exchange has orders with foreign clientOrderId (not grinder_ prefix)
When: planner runs
Then: foreign orders are not in diff (not cancelled, not matched)

### AC6: Live smoke 15-min lifecycle

Given: `--paper-spacing-bps 3 --paper-levels 5 --max-orders-per-run 1000`
When: run for 15 min on mainnet (ETHUSDT)
Then: `cancel > 0 OR replace > 0` at least once, FSM stable, sync healthy
