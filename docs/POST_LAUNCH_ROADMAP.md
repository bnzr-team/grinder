# Post-Launch Roadmap v1 (P1 Hardening Pack + P2 Target State)

> Status: ACTIVE (post Launch-12)
> Last updated: 2026-02-20
> Scope: single-venue stabilization (Binance USDT-M Futures) + target-state backlog shaping

## 0) What changed after Launch-12

- **Launch-01..Launch-12 are CLOSED** (Launch Readiness / P0).
- Post-launch work is organized as **Packs**:
  - **P1 Hardening Pack (ASAP post-launch):** ship stability + operability upgrades for real ACTIVE mainnet running.
  - **P2 Target State Pack:** breadth/features after single-venue is stable.
- Naming convention:
  - We keep **Launch-13/14/15** only for P1 (to preserve continuity),
  - Then move to **P2 Packs** (no more endless "Launch-XX" by default).

## 1) Current baseline (as of 2026-02-21)

- Milestones M1–M8: DONE
- Launch-01–12: DONE
- Launch-13 (FSM): DONE (main @ `7793045`)
- Launch-14 (SmartOrderRouter): DONE (main @ `e5b177c`, existing=None scope)
- P0 blockers: none
- P1 gaps open: 1 (PositionSyncer/RoundTrip)
- P2 backlog open: 12 (see section 3)

## 2) P1 Hardening Pack (ASAP post-launch)

### Goals (P1)
- Remove "implicit orchestration" and replace with explicit operational state machine.
- Reduce churn/cost/risk on write-path (amend vs cancel-replace decisions).
- Close loop on fill truth / position truth (PositionSyncer + RoundTrip accounting).
- Preserve project invariants: determinism gates, evidence artifacts, greppable evidence refs.

### Non-goals (P1)
- Multi-venue expansion (M9) — deferred until stable single-venue.
- Full ML policy integration — P2.
- Full backtest engine walk-forward — P2/Deferred.

---

## Launch-13: State Machine Orchestrator (FSM)

> **SSOT:** `docs/08_STATE_MACHINE.md` (Sec 8.9-8.14)

**Status:** ✅ COMPLETE (main @ `7793045`)

### Problem
We have regime classifier + scattered safety gates, but no centralized lifecycle FSM:
INIT → READY → DEGRADED → EMERGENCY, with explicit transitions, reasons, and ops actions.

### Delivered

- FSM tick wired into `LiveEngineV0.process_snapshot()` **before** action processing (Gate 6 sees current state)
- FSM metrics exported:
  - one-hot current state gauge
  - state duration gauge
  - transitions counter (from/to/reason)
  - blocked intents counter (state/intent)
- Gate 6 enforcement: intents blocked by FSM state return `FSM_STATE_BLOCKED`
- Operator override:
  - `GRINDER_OPERATOR_OVERRIDE` (normalized via `strip().upper()`)
  - documented runbook: `docs/runbooks/27_FSM_OPERATOR_OVERRIDE.md`
- Deterministic evidence artifacts on transitions (safe-by-default):
  - enable with `GRINDER_FSM_EVIDENCE`
  - `.sha256` is `sha256sum -c` compatible

### PR chain

- PR0 (#211) — Spec/ADR (SSOT wiring)
- PR1 (#213) — Pure FSM core + 74 deterministic tests (merged @ `897ca8b`)
- PR2 (#214) — Driver + metrics + Gate 6 (merged @ `89e329a`)
- PR3 (#215) — Real loop wiring + runtime signals (merged @ `232d07b`)
- PR4 (#216) — Operator override normalization + runbook (merged @ `6c37baf`)
- PR5 (#217) — Deterministic evidence artifacts (merged @ `7793045`)

---

## Launch-14: SmartOrderRouter (amend vs cancel-replace)

> **SSOT:** `docs/14_SMART_ORDER_ROUTER_SPEC.md` (decision matrix + invariants + PR plan)

**Status:** ✅ COMPLETE (main @ `e5b177c`) — existing=None scope, AMEND deferred

### Problem
We mostly do "cancel / place" patterns. Need router to decide:
- amend existing order vs cancel-replace vs noop
based on risk, tick/step constraints, exchange rules, and idempotency/retry behavior.

### Delivered

- `SmartOrderRouter` contract: `route(RouterInputs) -> RouterResult` with decision table
- Decision paths: CANCEL_REPLACE / BLOCK / NOOP (AMEND deferred — requires order state tracking)
- Spread-crossing detection, filter validation (tick/step/min_qty/min_notional), rate-limit budgets
- SOR wired into `LiveEngineV0._apply_sor()` with feature flag (default OFF, `GRINDER_SOR_ENABLED=1`)
- Router decision telemetry: `grinder_router_decision_total{decision, reason}`, `grinder_router_amend_savings_total`
- Fire drill: 4 drills (CANCEL_REPLACE/BLOCK/NOOP/metrics contract smoke), 20 PASS
- Runbook: `docs/runbooks/28_SOR_FIRE_DRILL.md`
- Evidence index updated (12 entries)

### PR chain

- PR0 (#219) — Spec/decision matrix + invariants (merged @ `8ff7339`)
- PR1 (#220) — Router core + table-driven tests (merged @ `d98008d`)
- PR2 (#221) — LiveEngine wiring + SOR metrics (merged @ `045e5c7`)
- PR3 (#222) — Fire drill + evidence + runbook (merged @ `e5b177c`)

---

## Launch-15: Fill Tracking v1.0 (PositionSyncer + RoundTrip)

### Problem
FillTracker exists, but we lack end-to-end "truth": position truth + round-trip accounting:
- ensure our internal state matches exchange position
- ensure we can attribute PnL / slippage / execution health per cycle

### Deliverables
- `PositionSyncer`:
  - compares local vs exchange position
  - reconciles safely (read-only safe path + controlled remediation path)
- `RoundTrip` accounting:
  - tracks open→close cycle, fees, slippage, realized PnL
  - emits metrics & evidence artifacts
- Alerts/runbooks for mismatch thresholds.

### Acceptance Criteria (MUST)
- Deterministic fixtures for sync decisions (no flakiness).
- Any remediation is gated (kill-switch / drawdown / leader-only if HA).
- Evidence artifacts produced for mismatch events (summary + sha256sums) and surfaced via `EVIDENCE_REF`.
- Ops triage includes pointers to roundtrip evidence.

### Suggested PR breakdown
- PR1: PositionSyncer core + tests + metrics/logs
- PR2: RoundTrip model + integration + dashboards/alerts
- PR3: drills + runbooks + ops entrypoint wiring

---

### P1 Definition of Done (Pack-level)
- All 3 Launches (13/14/15) merged to main.
- CI green on all PRs (required checks).
- Post-merge sanity scripts exist and pass (or are folded into existing ops triage).
- Runbooks updated:
  - "what to run", "what evidence to paste", "how to respond"
- Evidence:
  - artifacts are produced where relevant
  - `EVIDENCE_REF` lines are present and greppable for any incident-relevant mode

## 3) P2 Target State Pack (Backlog shaping)

> These are **post single-venue stabilization**. We keep them in a ranked list with dependencies.

### P2 Backlog (12 gaps)
1. Toxicity formulas expansion (VPIN, Kyle, Amihud, OFI) — depends on feature plumbing
2. Grid policy library expansion (Trend, LiqCatcher, etc.) — depends on policy interfaces
3. Backtest engine (walk-forward + cost model) — deferred (big)
4. Fill probability model — depends on fill dataset + roundtrip truth
5. Portfolio risk manager (beta-adjusted, concentration) — depends on accounting/positions
6. Consecutive loss limit — depends on roundtrip outcomes
7. ML training pipeline real implementation — depends on dataset/feature store maturity
8. ML policy integration (signal -> grid params) — depends on stable signal contracts
9. ML drift detection — depends on online metrics + dataset snapshots
10. FeatureStore module/service — depends on dataset manifest pipeline
11. Advanced features (OFI, CVD, VAMP, multi-TF) — depends on feed + storage
12. Multi-venue (Bybit, OKX, COIN-M) — deferred until stable single-venue

### P2 Output format (when we start)
- P2 will be tracked as **Packs** (e.g., "P2-ML Pack", "P2-Policy Pack"), each with 2–5 PR max.
- Each pack must define:
  - acceptance criteria
  - evidence requirements
  - runbook updates (if ops-facing)

## 4) PR count expectations (so we don't get lost)

- P1 Hardening Pack:
  - Launch-13: 6 PR (PR0–PR5, shipped)
  - Launch-14: 2–3 PR
  - Launch-15: 2–3 PR
  - **Expected total: 6–9 PR** (strict) or **9–12 PR** (full drills/docs)

- P2:
  - intentionally unbounded, but we enforce "pack size" to keep it readable.

## 5) Operating principle

No "works/implemented" claims without:
- CI green proof
- reproducible commands output
- evidence artifacts where relevant
- and explicit wiring in docs/runbooks
