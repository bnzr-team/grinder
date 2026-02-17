# ROADMAP — Grinder

This file tracks **plan + progress**.

- **Source of truth for current implementation:** `docs/STATE.md`
- **Why key choices were made:** `docs/DECISIONS.md`
- Specs in `docs/*` describe **target behavior** unless `STATE.md` says implemented.

Last updated: 2026-02-12

---

## 0) Current Main Truth State

This section reflects **what is verified and merged on main** as of PR #117.

### Milestone Status

| Milestone | Status | Date |
|-----------|--------|------|
| M1 — Vertical Slice v0.1 | ✅ Done | 2026-01-31 |
| M2 — Beta v0.5 | ✅ Done | 2026-02-01 |
| M3 — Live Reconciliation | ✅ Done | 2026-02-07 |
| M4 — Ops Hardening | ✅ Done | 2026-02-07 |
| M5 — Observability Polish | ✅ Done | 2026-02-07 |
| M6 — HA Leader Election | ✅ Done | 2026-02-08 |
| M7 — Smart Grid v2.0 | ✅ Done | — |
| M8 — ML Integration | 🚧 In Progress | — |
| M9 — Multi-venue | 🔜 Planned | — |

### Stage D/E E2E Mainnet Verification

| Stage | Status | Description | PR |
|-------|--------|-------------|-----|
| Stage D | ✅ E2E mainnet | `execute_cancel_all` — grinder_ orders cancelled | PR #102 |
| Stage E | ✅ E2E mainnet | `execute_flatten` — position flattened | PR #103 → `9572fd7` |
| Docs | ✅ Runbook-ready | STATE.md updated with tight budgets | PR #104 → `4434284` |

### M4–M6 Post-Stage-E Completion

| Milestone | Description | PR |
|-----------|-------------|-----|
| M4.1 | Artifacts run-dir + fixed filenames | PR #107 → `83630e4` |
| M4.2 | Budget state lifecycle (`--reset-budget`) | PR #108 → `4e47814` |
| M4.3 | Low-notional runbook (exchangeInfo procedure) | PR #109 → `6c03f1b` |
| M5 | Dashboards + alerts for reconcile actions/budgets | PR #110 → `49c4b21` |
| M6 | HA Leader-Only Remediation (LC-20) | PR #111 → `b7b6ab8` |
| M6 fixes | Smoke determinism + docs | PR #112–#115 |
| Docs | README SSOT alignment (M1-M6 status) | PR #117 → `e501357` |

### Key Achievements (LC-* series)

- **LC-12:** `parse_client_order_id()` — fixed `split("_")` bug for cancel routing
- **LC-18:** 5-mode staged rollout (detect_only → plan_only → blocked → execute_cancel_all → execute_flatten)
- **LC-20:** HA leader-only remediation — Redis-backed leader election with TTL locks
- **Connector hardening:** Timeouts, retries, idempotency, circuit breaker, connector metrics (see STATE.md)
- **Safety gates verified:**
  - `ALLOW_MAINNET_TRADE=1` hard gate for execute modes
  - `REMEDIATION_*_ALLOWLIST` for strategy + symbol filtering
  - Budget caps: `MAX_CALLS_PER_DAY`, `MAX_NOTIONAL_PER_DAY`
- **Metrics contract:** SSOT in `live_contract.py`, graceful without redis
- **HA metrics:** `grinder_ha_is_leader` gauge, `action_blocked_total{reason="not_leader"}`
- **Ops hygiene:** Artifacts run-dir, budget lifecycle, low-notional runbook

---

## 1) Historical Status

### Foundation / Infrastructure — Done
- ✅ PR #2–#9: infrastructure fixes (CLI entrypoints, soak runner, docker/grafana, docs truth, proof guards)
- ✅ PR #10: `STATE.md` updated to match reality
- ✅ PR #11: CODEOWNERS added for critical paths

---

## 2) Completed Milestones

### M1 — Vertical Slice v0.1 (Replay-first) — ✅ Done 2026-01-31
**Goal:** fixture → prefilter → policy → order intents → execution stub → metrics/logs → deterministic replay

**Governing docs**
- `docs/11_BACKTEST_PROTOCOL.md` (determinism rules)
- `docs/04_PREFILTER_SPEC.md` (gating intent)
- `docs/07_GRID_POLICY_LIBRARY.md` (policy intent)
- `docs/09_EXECUTION_SPEC.md` (intent→execution contract)
- `docs/10_RISK_SPEC.md` (limits/kill-switch expectations)
- `docs/13_OBSERVABILITY.md` (metrics/logging expectations)

**Work items (all merged)**
- ✅ PR-013: Domain contracts (events/state/order_intents) — merged 2026-01-31
- ✅ PR-014: Prefilter v0 (rule-based gating) — merged 2026-01-31
- ✅ PR-015: Adaptive Controller spec + unicode scanner — merged 2026-01-31
- ✅ PR-016: GridPolicy v0 (static symmetric grid) — merged 2026-01-31 (GitHub PR #17)
- ✅ PR-017: Execution stub v0 (apply intents, no exchange) — merged 2026-01-31 (GitHub PR #20)
- ✅ PR-018: CLI wiring for one end-to-end replay run — merged 2026-01-31

---

### M2 — Beta v0.5 (Paper Loop + Gating) — ✅ Done 2026-02-01
**Goal:** paper loop with gating + observability maturity

**Work items (all merged)**
- ✅ Adaptive Controller spec (docs-only): `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` — merged 2026-01-31
- ✅ PR-019: Paper Loop v0 + Gating (rate limit + risk gate) — merged 2026-01-31
- ✅ PR-020: Gating metrics contract (GatingMetrics, labels, contract tests) — merged 2026-01-31
- ✅ PR-021: Observability /metrics endpoint (gating metrics via HTTP) — merged 2026-01-31
- ✅ PR-022: Allowed-orders fixture + fill coverage — merged 2026-01-31
- ✅ PR-023: Fill simulation + position tracking + PnL ledger — merged 2026-01-31 (GitHub PR #32)
- ✅ PR-024: Backtest protocol script + contract tests — merged 2026-01-31 (GitHub PR #33)
- ✅ PR-025: ToxicityGate v0 (spread spike + price impact detection) — merged 2026-01-31 (GitHub PR #34)
- ✅ PR-026: Top-K prefilter v0 (volatility scoring, K=3 default) — merged 2026-01-31
- ✅ PR-027: Adaptive Controller v0 (rule-based modes: BASE/WIDEN/TIGHTEN/PAUSE) — merged 2026-02-01 (GitHub PR #35)
- ✅ PR-028: Observability stack v0 (Prometheus + Grafana + alerts) — merged 2026-02-01 (GitHub PR #37)
- ✅ PR-029: docs/DEV.md (developer environment setup guide) — merged 2026-02-01 (GitHub PR #36)

**M2 DoD achieved:**
- Paper loop with fills, positions, PnL ✓
- Gating: rate limit + risk + toxicity ✓
- Top-K prefilter working on fixtures ✓
- Adaptive Controller v0 (rule-based) ✓
- Backtest protocol on 5 fixtures ✓
- Observability stack with dashboards + alerts ✓

---

### M3 — Live Reconciliation (LC-* series) — ✅ Done 2026-02-07
**Goal:** Live reconciliation with active remediation on Binance Futures USDT-M

**Note:** This milestone supersedes the original "M3 — Production v1.0" placeholder.
See ADR-053 for rationale.

**Work items (all merged, PR #37–#104)**
- ✅ LC-04: BinanceExchangePort v0.2 (Spot, testnet + mainnet guards)
- ✅ LC-05: LiveEngine v0 (write-path wiring with arming model)
- ✅ LC-06: LiveFeed v0 (read-path: WS → Snapshot → Features)
- ✅ LC-08b-F: BinanceFuturesPort v0.1 (Futures USDT-M)
- ✅ LC-09a: FuturesUserDataWsConnector (user-data stream)
- ✅ LC-09b: Passive reconciliation (expected vs observed state)
- ✅ LC-10: Active remediation (cancel_all + flatten actions)
- ✅ LC-11: ReconcileRunner wiring + routing policy
- ✅ LC-12: Configurable order identity (`parse_client_order_id()`)
- ✅ LC-13: E2E smoke harness (3 scenarios)
- ✅ LC-14a/b: ReconcileLoop (background thread + real sources)
- ✅ LC-15a: Enablement ceremony (5-stage rollout)
- ✅ LC-15b: Reconcile observability (26 metrics + 7 alerts + SLOs)
- ✅ LC-17: Credentialed real-source smoke (detect-only)
- ✅ LC-18: Staged rollout modes (5 modes)
- ✅ Connector hardening: Timeouts, retries, idempotency, circuit breaker (via LC-* PRs)

**M3 DoD achieved:**
- Stage D E2E: `execute_cancel_all` verified on mainnet (PR #102) ✓
- Stage E E2E: `execute_flatten` verified on mainnet (PR #103) ✓
- All safety gates verified (ALLOW_MAINNET_TRADE, allowlists, budgets) ✓
- Metrics contract SSOT in `live_contract.py` ✓
- Runbook-ready commands in STATE.md (PR #104) ✓

---

## 3) Completed Milestones (Post-Stage-E) — M4/M5/M6

> **Note:** M4, M5, M6 were completed in PR #107–#115, #117. Below is preserved for historical reference.

### M4 — Ops Hardening (P1/P2) — ✅ Done 2026-02-07
**Goal:** Operational hygiene for production readiness

#### M4.1 — Artifacts Hygiene
**Deliverables:**
- Run-directory structure: `$GRINDER_ARTIFACTS_DIR/YYYY-MM-DD/run_<ts>/`
- Fixed filenames inside run-dir: `stdout.log`, `audit.jsonl`, `metrics.prom`, `metrics_summary.json`, `budget_state.json`
- TTL policy: 7–14 days retention, older run-dirs rotated/archived

**Acceptance / DoD:**
- If `--audit-out`/`--metrics-out` are NOT provided and `GRINDER_ARTIFACTS_DIR` is set, outputs go to run-dir by default; explicit paths override
- Rotation script or cron job documented in runbook
- No `/tmp` artifacts in operator workflow

**Required proof:**
- `ls -laR $GRINDER_ARTIFACTS_DIR/` showing run-dir structure + fixed filenames
- Runbook section for rotation
- `pytest tests/unit/test_audit.py` passes

---

#### M4.2 — BudgetState Policy
**Deliverables:**
- Document when to delete `BUDGET_STATE_PATH` (first run clean vs multi-run persist)
- Add `--reset-budget` CLI flag to `run_live_reconcile.py`
- Add warning log if budget file older than 24h

**Acceptance / DoD:**
- Runbook section: "Budget State Management"
- `--reset-budget` flag implemented and tested
- Stale budget warning appears in logs

**Required proof:**
- `python -m scripts.run_live_reconcile --help` shows `--reset-budget`
- Log output showing stale budget warning
- `pytest tests/unit/test_budget.py` passes

---

#### M4.3 — Runbook: Stage E on Non-BTC Symbols
**Deliverables:**
- Document procedure to query `GET /fapi/v1/exchangeInfo` and extract min notional per symbol
- Example symbols with low min notional (verify live before use)
- Example command for micro-position testing (~$10–20)
- Update `scripts/place_test_position.py` safety cap if needed

**Acceptance / DoD:**
- Runbook section: "Testing with Low-Notional Symbols" with query procedure
- At least 2 example symbols with instructions to verify min notional live
- Example command runnable without $100+ exposure

**Required proof:**
- `curl -s 'https://fapi.binance.com/fapi/v1/exchangeInfo' | jq '.symbols[] | select(.symbol=="DOGEUSDT") | .filters'` (or equivalent)
- Runbook diff with query procedure + example command
- (Optional) E2E test on low-notional symbol

---

### M5 — Observability Polish — ✅ Done 2026-02-07
**Goal:** Production-grade dashboards and alerting

**Deliverables:**
- Dashboard panel: budget remaining (calls/notional)
- Dashboard panel: `action_executed_total` time series
- SLO/Runbook binding: "Budget exhausted → what to do"
- Alert: `ReconcileBudgetExhausted` (critical)

**Acceptance / DoD:**
- Grafana dashboard updated with budget panels
- Alert rule added to `monitoring/alert_rules.yml`
- Runbook section: "Budget Exhausted Response"

**Required proof:**
- `docker compose -f docker-compose.observability.yml up --build -d`
- `./scripts/docker_smoke_observability.sh` passes
- `curl -sf localhost:9090/metrics | rg "grinder_reconcile_budget"` shows budget metrics
- `promtool check rules monitoring/alert_rules.yml` passes
- Runbook diff
- (Optional) Screenshot of Grafana dashboard with budget panels

---

### M6 — HA / Leader Election (LC-20) — ✅ Done 2026-02-08
**Goal:** Safe multi-instance deployment where only leader can remediate

**Deliverables:**
- Leader election mechanism (e.g., Redis lock, etcd, Kubernetes lease)
- `ReconcileLoop` respects leader status (only leader executes, followers detect/plan)
- Metric: `grinder_ha_is_leader` gauge
- Graceful leader handoff on shutdown

**Acceptance / DoD:**
- 2-instance test: only 1 instance executes remediation
- Failover test: leader dies → follower takes over within 30s
- No split-brain: 0 duplicate executions

**Required proof:**
- Integration test with 2 instances
- Log output showing leader election
- Metric `grinder_ha_is_leader` toggling
- `pytest tests/integration/test_ha_leader.py` passes

---

## 4) Traceability Matrix

| Milestone | Scope / Deliverable | Governing docs | Required proofs/tests | Outputs / artifacts |
|---|---|---|---|---|
| M1 — Vertical Slice | Fixture pipeline | STATE.md, 11_BACKTEST_PROTOCOL.md | pytest, mypy, verify_replay_determinism | Replay digest |
| M2 — Beta v0.5 | Paper loop + gating | STATE.md, 13_OBSERVABILITY.md | pytest, mypy, docker smoke | Dashboards |
| M3 — Live Reconciliation | Active remediation | STATE.md, ADR-042–052 | pytest, mypy, E2E mainnet verification | Audit JSONL, metrics |
| M4 — Ops Hardening | Artifacts, budget, runbooks | STATE.md, runbooks/ | pytest, runbook review | Stable artifacts |
| M5 — Observability Polish | Dashboards, alerts, SLOs | STATE.md, 13_OBSERVABILITY.md | promtool, Grafana screenshots | Alert rules |
| M6 — HA / Leader Election | Multi-instance safety | STATE.md | Integration tests, failover test | HA runbook |
| M7 — Smart Grid v2.0 | L2-aware + DD Allocator | smart_grid/SPEC_V2_0.md | L2 fixtures, allocator tests | L2 digest |
| M8 — ML Integration | Inference pipeline | 12_ML_SPEC.md | Pinned artifacts, determinism | Calibration artifacts |
| M9 — Multi-venue | COIN-M + other exchanges | — | Per-venue smoke tests | Venue adapters |

---

## 5) Planned Milestones (M7–M9)

### M7 — Smart Grid v2.0 (L2-aware + DD Allocator) — ✅ Done

**Goal:** L2 order book integration with depth-aware sizing and portfolio-level drawdown allocation

**Governing docs:**
- `docs/smart_grid/SPEC_V2_0.md` — target spec
- ADR-031: Auto-Sizing v1 (risk-budget-based) — foundation
- ADR-032: DD Allocator v1 (portfolio-to-symbol) — foundation
- ADR-033: Drawdown Guard Wiring v1 — foundation

**Key deliverables:**
- L2 order book snapshots in replay/paper/live pipelines
- Depth-aware impact/spread gating and sizing
- Drawdown allocator distributing budget across Top-K symbols
- Deterministic walk-the-book execution model

**Implementation status:**

| Sub-milestone | Code | ADR | Unit Tests | Digest Fixture |
|---------------|------|-----|------------|----------------|
| M7-03: L2 gating | ✅ | ADR-057 | ✅ | ✅ `sample_day_l2_gating` |
| M7-04: DD budget ratio | ✅ | ADR-058 | ✅ | ✅ (covered by L2 gating) |
| M7-05: Qty constraints | ✅ | ADR-059 | ✅ | ✅ `sample_day_constraints` |
| M7-06: ConstraintProvider | ✅ | ADR-060 | ✅ | ✅ (covered by constraints) |
| M7-07: ExecutionEngineConfig | ✅ | ADR-061 | ✅ | ✅ (wiring tested in all M7 fixtures) |
| M7-08: TTL/Refresh | ✅ | ADR-063 | ✅ | ✅ (unit tests sufficient) |
| M7-09: L2 Exec Guard | ✅ | ADR-062 | ✅ | ✅ `sample_day_l2_exec_guard` |

**PR #137:** Added 3 digest-gated fixtures covering M7 features.
All 11 determinism fixtures pass (`verify_determinism_suite.py`).

---

### M8 — ML Integration — 🚧 In Progress

**Goal:** ML-assisted regime classification and parameter tuning

**Governing docs:**
- `docs/12_ML_SPEC.md` — target spec

**Key deliverables:**
- Offline calibration pipeline with pinned artifacts by hash
- Inference integration with determinism tests
- Feature store for training data

**Current state (2026-02-16):**
- M8-00: Spec — ✅ Done (PR #134)
- M8-01: Stubs — ✅ Done (PR #140, #141, #142, #143)
- M8-02a: Artifact plumbing — ✅ Done (PR #144)
- M8-02b: Shadow mode — ✅ Done (PR #145)
- M8-02c: Active inference (ADR-065) — ✅ Done (PR #146, #147, #148, #149)
- M8-02d: Latency histogram — ✅ Done (PR #151)
- M8-02e: Grafana dashboards — ✅ Done (PR #154)
- M8-03a: Artifact pack + build CLI — ✅ Done (PR #150)
- M8-03b-1: Training pipeline MVP — ✅ Done (PR #152)
- M8-03b-2: Runtime integration + determinism — ✅ Done (PR #153)
- M8-03c-1a: Registry spec + runbook — ✅ Done (PR #155)
- M8-03c-1b: Registry implementation — ✅ Done (PR #157)
- M8-03c-2: PaperEngine config wiring — ✅ Done (PR #158)
- M8-03c-3: Promotion CLI + history — ✅ Done (PR #159)
- M8-04: Feature store spec — ✅ Done (docs/18_FEATURE_STORE_SPEC.md)
- Remaining: Feature store implementation (M8-04a–M8-04e, see spec for sub-milestones)

---

### M9 — Multi-venue — 🔜 Planned

**Goal:** Extend beyond Binance USDT-M Futures

**Candidates:**
- Binance COIN-M Futures
- Other CEXs (Bybit, OKX)
- DEXs (future consideration)

**Key deliverables:**
- Venue abstraction layer
- Per-venue adapters (port implementations)
- Cross-venue reconciliation

**Current state:** Out of scope until M7/M8 complete.

---

## 6) LC-Series Index

This section documents the LC-* (Live Connector) series for traceability.

### LC Numbering

| LC | ADR | Description | Status | Notes |
|----|-----|-------------|--------|-------|
| LC-01 | ADR-029 | LiveConnector v0 SafeMode | ✅ Done | |
| LC-02 | ADR-030 | Paper Write-Path v0 | ✅ Done | |
| LC-03 | ADR-034 | Paper Realism (tick-delay) | ✅ Done | |
| LC-04 | ADR-035 | BinanceExchangePort v0.1 (Spot) | ✅ Done | |
| LC-05 | ADR-036 | LiveEngineV0 wiring | ✅ Done | |
| LC-06 | ADR-037 | LiveFeed read-path | ✅ Done | |
| LC-07 | ADR-038 | Testnet smoke harness | ✅ Done | |
| LC-08b | ADR-039 | Spot mainnet smoke | ✅ Done | |
| LC-08b-F | ADR-040 | Futures USDT-M mainnet smoke | ✅ Done | |
| LC-09a | ADR-041 | FuturesUserDataWsConnector | ✅ Done | |
| LC-09b | ADR-042 | Passive reconciliation | ✅ Done | |
| LC-10 | ADR-043 | Active remediation (9 gates) | ✅ Done | |
| LC-11 | ADR-044 | ReconcileRunner wiring | ✅ Done | |
| LC-11b | ADR-046 | Audit JSONL | ✅ Done | |
| LC-12 | ADR-045 | Configurable order identity | ✅ Done | |
| LC-13 | ADR-047 | E2E smoke harness | ✅ Done | |
| LC-14a | ADR-048 | ReconcileLoop wiring | ✅ Done | |
| LC-14b | ADR-049 | Real sources wiring | ✅ Done | |
| LC-15a | ADR-050 | Staged enablement ceremony | ✅ Done | |
| LC-15b | ADR-051 | Reconcile alerts/SLOs | ✅ Done | |
| LC-16 | — | Observability hardening | ✅ Done | No ADR: polish, no contract change |
| LC-17 | — | Credentialed real-source smoke | ✅ Done | Script-only |
| LC-18 | ADR-052 | 5-mode staged rollout | ✅ Done | |
| LC-19 | — | — | ⏭️ Skipped | Reserved, never used |
| LC-20 | ADR-054 | HA leader-only remediation | ✅ Done | PR #111 |
| LC-21 | ADR-055 | L1 WebSocket integration | ✅ Done | PR #119 |
| LC-22 | ADR-056 | LIVE_TRADE write-path | ✅ Done | PR #120 |
| LC-23 | — | Enablement runbook (docs-only) | ✅ Done | PR #122, no ADR |

---

## 7) Definition of Done (DoD) — Historical Reference

See sections below for M1/M2 PR-level DoD. These are preserved for reference.

### PR-013 — Domain contracts (events/state/order_intents)
**DoD**
- Introduce minimal typed contracts used across the pipeline
- No business logic yet — only contracts + tests
- Update `STATE.md`

**Proof**
- `PYTHONPATH=src python -m pytest -q`
- `PYTHONPATH=src python -m mypy .`

---

### PR-014 — Prefilter v0 (rule-based)
**DoD**
- Implement a rule-based gate that returns ALLOW/BLOCK + reason
- Unit tests: allow + block paths
- Update `STATE.md`

**Proof**
- `pytest`, `mypy`
- If fixtures touched: `python -m scripts.verify_replay_determinism ...`

---

### PR-019 — Paper Loop v0 + Gating
**DoD**
- PaperExecutionPort/PaperEngine: execution without real orders
- Gating v0: at least 2 gates (rate limit + risk limit)
- CLI: `grinder paper --fixture <path>`
- Unit tests for gates (allow/block paths)
- E2E test for paper loop with deterministic digest
- Update `STATE.md`

**Proof**
- `PYTHONPATH=src python3 -m pytest -q`
- `ruff check .` + `ruff format --check .`
- `mypy .`
- `python3 -m scripts.check_unicode --all`
