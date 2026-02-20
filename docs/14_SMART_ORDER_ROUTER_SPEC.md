# 14 — SmartOrderRouter Spec (amend vs cancel-replace)

> **Status:** DRAFT (Launch-14 PR0)
> **Last updated:** 2026-02-20
> **SSOT:** This document. Referenced from `docs/POST_LAUNCH_ROADMAP.md` (Launch-14 section).
> **Prerequisite:** `docs/09_EXECUTION_SPEC.md` (Sec 9.3 — target-state pseudocode).

---

## 14.1 Problem Statement

The current `ExecutionEngine` produces `PLACE` and `CANCEL` actions. When the grid plan changes
(price drift, spacing adjustment, adaptive controller mode switch), the engine cancels all
existing orders and places new ones. This has three costs:

1. **Churn cost:** Cancel + place = 2 API calls per level vs 1 amend call.
2. **Exposure gap:** Between cancel and place, we have no order on that level — brief
   unhedged window.
3. **Rate-limit risk:** Binance Futures rate limits are per-UID. Churn amplifies request
   count, raising the probability of 429/IP-ban during volatile markets.

**SmartOrderRouter** (SOR) sits between `ExecutionEngine` action output and the
`ExchangePort` boundary, deciding the **minimal-risk execution plan** for each grid
update cycle.

---

## 14.2 Scope

### In scope (Launch-14)
- Decision logic: AMEND vs CANCEL_REPLACE vs NOOP vs BLOCK.
- Constraint validation: tickSize, stepSize, minQty, minNotional.
- Drawdown-gate integration: SOR must never increase risk when drawdown gate is active.
- Telemetry: decision counter, reason codes, structured log.
- Deterministic unit tests (table-driven, no flakiness).

### Out of scope (deferred)
- Batch amend API (`batchOrders` endpoint) — future optimization.
- Fill-probability-weighted routing — depends on P2 fill model.
- Multi-venue routing — deferred to M9.
- Retry/backoff inside SOR — handled by `IdempotentPort` layer.

---

## 14.3 Inputs

| Input | Type | Source | Description |
|-------|------|--------|-------------|
| `intent` | `OrderIntent` | Gate 6 / FSM | INCREASE_RISK, REDUCE_RISK, CANCEL |
| `desired` | `GridLevel` | ExecutionEngine | Target price + qty for this level |
| `existing` | `OrderRecord \| None` | Order book state | Currently open order on this level (if any) |
| `constraints` | `SymbolConstraints` | ConstraintProvider | stepSize, minQty from exchangeInfo |
| `tick_size` | `Decimal` | exchangeInfo PRICE_FILTER | Minimum price increment |
| `min_notional` | `Decimal` | exchangeInfo MIN_NOTIONAL | Minimum order value |
| `spread_bps` | `float` | FeatureEngine | Current bid-ask spread in basis points |
| `drawdown_breached` | `bool` | DrawdownGuard | Whether drawdown gate is active |
| `position_side` | `str` | PositionTracker | Current net position direction |

---

## 14.4 Outputs

| Output | Type | Description |
|--------|------|-------------|
| `decision` | `RouterDecision` | Enum: `AMEND`, `CANCEL_REPLACE`, `NOOP`, `BLOCK` |
| `reason` | `str` | Machine-readable reason code (see 14.6) |
| `actions` | `list[OrderAction]` | Concrete actions to execute (0, 1, or 2) |

### RouterDecision Enum

```python
class RouterDecision(str, Enum):
    AMEND = "AMEND"                    # Modify existing order in-place
    CANCEL_REPLACE = "CANCEL_REPLACE"  # Cancel existing + place new
    NOOP = "NOOP"                      # No change needed
    BLOCK = "BLOCK"                    # Action blocked by safety gate
```

---

## 14.5 Decision Matrix (Core)

This is the **central decision table**. Each row is a condition set; first match wins (top-down priority).

| # | existing? | drawdown_breached | intent | price_delta_bps | qty_changed | constraint_ok | Decision | Reason |
|---|-----------|-------------------|--------|-----------------|-------------|---------------|----------|--------|
| 1 | any | `True` | INCREASE_RISK | any | any | any | **BLOCK** | `DRAWDOWN_GATE_ACTIVE` |
| 2 | any | any | CANCEL | any | any | any | **CANCEL_REPLACE** | `EXPLICIT_CANCEL` |
| 3 | `None` | any | any | any | any | `True` | **CANCEL_REPLACE** | `NO_EXISTING_ORDER` |
| 4 | `None` | any | any | any | any | `False` | **BLOCK** | `CONSTRAINT_VIOLATION` |
| 5 | yes | any | any | `0` | `False` | any | **NOOP** | `NO_CHANGE` |
| 6 | yes | any | any | `<= AMEND_THRESHOLD` | any | `True` | **AMEND** | `SMALL_PRICE_DELTA` |
| 7 | yes | any | any | `> AMEND_THRESHOLD` | any | `True` | **CANCEL_REPLACE** | `LARGE_PRICE_DELTA` |
| 8 | yes | any | any | any | `True` | `True` | **AMEND** | `QTY_CHANGE_ONLY` |
| 9 | yes | any | any | any | any | `False` | **BLOCK** | `CONSTRAINT_VIOLATION` |

### Decision Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `AMEND_THRESHOLD_BPS` | `20` | Price delta threshold: below = amend, above = cancel-replace |

### Column Definitions

- **price_delta_bps**: `abs(desired.price - existing.price) / existing.price * 10_000`
- **qty_changed**: `desired.qty != existing.qty` (after step_size rounding)
- **constraint_ok**: `desired.qty >= minQty AND desired.price % tickSize == 0 AND desired.qty * desired.price >= minNotional`

### Decision Priority Notes

1. Safety gates (rows 1, 4, 9) always take precedence over optimization.
2. NOOP (row 5) avoids unnecessary API calls when nothing changed.
3. AMEND (rows 6, 8) is preferred when safe — reduces churn.
4. CANCEL_REPLACE (rows 2, 3, 7) is the fallback for large changes or missing orders.

---

## 14.6 Reason Codes

| Code | Decision | Description |
|------|----------|-------------|
| `DRAWDOWN_GATE_ACTIVE` | BLOCK | Drawdown guard prevents risk increase |
| `EXPLICIT_CANCEL` | CANCEL_REPLACE | Caller explicitly requested cancel |
| `NO_EXISTING_ORDER` | CANCEL_REPLACE | No order to amend — must place new |
| `CONSTRAINT_VIOLATION` | BLOCK | tickSize/stepSize/minQty/minNotional violated |
| `NO_CHANGE` | NOOP | Desired == existing (within tolerance) |
| `SMALL_PRICE_DELTA` | AMEND | Price delta within amend threshold |
| `LARGE_PRICE_DELTA` | CANCEL_REPLACE | Price delta exceeds amend threshold |
| `QTY_CHANGE_ONLY` | AMEND | Only quantity changed, price identical |

---

## 14.7 Constraint Validation

SOR validates constraints **before** deciding. If constraints fail, the decision is BLOCK
regardless of other inputs.

### Constraint Checks

```
1. tick_size_ok   = (desired.price % tick_size == 0)
2. step_size_ok   = (desired.qty == floor_to_step(desired.qty, step_size))
3. min_qty_ok     = (desired.qty >= min_qty)
4. min_notional_ok = (desired.qty * desired.price >= min_notional)
```

All four must pass for `constraint_ok = True`.

### Integration with ConstraintProvider

SOR does **not** fetch constraints itself. It receives `SymbolConstraints` + `tick_size` +
`min_notional` as inputs. The caller (ExecutionEngine or live loop) is responsible for
populating these from `ConstraintProvider`.

---

## 14.8 Invariants

These **must** hold at all times. Tests enforce them.

| # | Invariant | Enforcement |
|---|-----------|-------------|
| I1 | SOR never increases risk when `drawdown_breached=True` | Row 1 blocks INCREASE_RISK unconditionally |
| I2 | SOR never produces actions that violate exchange constraints | Constraint check runs before decision |
| I3 | SOR decisions are deterministic (same inputs = same output) | Pure function, no side effects, no I/O |
| I4 | SOR never modifies the FSM or any upstream state | Input-only contract; returns decision tuple |
| I5 | Every decision emits a reason code | Reason is required field in output |
| I6 | AMEND is only chosen when exchange supports it | Capability flag in SOR config (default: True for Binance Futures) |

---

## 14.9 Telemetry

### Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_router_decision_total` | Counter | `decision`, `reason` | Router decisions by type and reason |
| `grinder_router_amend_savings_total` | Counter | — | Number of amends that avoided cancel+place |
| `grinder_router_constraint_violations_total` | Counter | `check` | Constraint violations by type |

### Structured Log

On every decision:
```
logger.info(
    "SOR decision",
    extra={
        "decision": decision.value,
        "reason": reason,
        "symbol": symbol,
        "price_delta_bps": price_delta_bps,
        "qty_changed": qty_changed,
        "drawdown_breached": drawdown_breached,
    },
)
```

---

## 14.10 Integration Point

```
ExecutionEngine.reconcile()
    │
    ├── for each level: compute desired GridLevel
    │
    ├── SmartOrderRouter.decide(intent, desired, existing, constraints, ...)
    │   └── returns (decision, reason, actions)
    │
    ├── if BLOCK: skip + log
    ├── if NOOP: skip
    ├── if AMEND: call ExchangePort.amend_order(...)
    └── if CANCEL_REPLACE: call ExchangePort.cancel_order() + place_order()
```

### ExchangePort Extension

`ExchangePort` protocol will need a new method:

```python
def amend_order(self, order_id: str, price: Decimal, quantity: Decimal) -> bool:
    """Amend an existing order's price and/or quantity."""
    ...
```

This maps to Binance Futures `PUT /fapi/v1/order` (modify order endpoint).

---

## 14.11 PR Breakdown

| PR | Scope | Deliverables |
|----|-------|--------------|
| **PR0** (this) | Spec + decision matrix | This document |
| **PR1** | Router contract + tests | `SmartOrderRouter` pure logic, `RouterDecision` enum, decision table unit tests (table-driven), constraint validation |
| **PR2** | Integration + metrics | Wire into ExecutionEngine, `amend_order()` on ExchangePort/BinanceFuturesPort, router metrics, structured logs |
| **PR3** | Fire drill + runbook | Simulation fixture showing amend-chosen vs cancel-replace, runbook for router decision debugging, ops triage updates |

### PR1 Acceptance Criteria

- [ ] `SmartOrderRouter.decide()` is a pure function (no I/O, no side effects).
- [ ] All 9 decision matrix rows covered by parametrized tests.
- [ ] Constraint validation tests (tick_size, step_size, min_qty, min_notional).
- [ ] Invariants I1–I6 tested explicitly.
- [ ] `RouterDecision` and reason codes match this spec exactly.

### PR2 Acceptance Criteria

- [ ] `ExchangePort.amend_order()` added to protocol + implemented in `BinanceFuturesPort`.
- [ ] `ExecutionEngine` uses SOR for order reconciliation.
- [ ] Metrics emitted: `grinder_router_decision_total`, `grinder_router_amend_savings_total`.
- [ ] `metrics_contract.py` updated with router patterns.
- [ ] Structured logging on every decision.

### PR3 Acceptance Criteria

- [ ] Fire drill fixture: shows SOR choosing AMEND for small delta, CANCEL_REPLACE for large delta.
- [ ] Runbook: how to inspect router decisions, what to do on unexpected BLOCK.
- [ ] Ops triage updated with SOR decision pointers.

---

## 14.12 Definition of Done (Launch-14)

- All PRs (PR0–PR3) merged to main.
- CI green on all PRs.
- Decision matrix tested end-to-end with deterministic fixtures.
- Router never increases risk under drawdown gate (invariant I1 proven by test).
- Metrics visible in Prometheus format.
- Runbook updated with SOR decision debugging.

---

## 14.13 Open Questions (to resolve in PR1)

1. **Binance Futures amend latency:** Does `PUT /fapi/v1/order` have different rate limits
   than cancel+place? Need to check docs before PR2 integration.
2. **Partial fill handling:** If existing order is partially filled, should SOR allow amend?
   Conservative answer: CANCEL_REPLACE (avoid amending a partially-filled order).
3. **Price tolerance for NOOP:** Should we have a sub-tick tolerance where price delta < 1 tick
   is treated as NOOP? Current spec: exact match only (row 5).
