# ROADMAP — Grinder

This file tracks **plan + progress**.

- **Source of truth for current implementation:** `docs/STATE.md`
- **Why key choices were made:** `docs/DECISIONS.md`
- Specs in `docs/*` describe **target behavior** unless `STATE.md` says implemented.

Last updated: 2026-02-07

---

## 0) Current Main Truth State

This section reflects **what is verified and merged on main** as of PR #104.

### Completed Milestones

| Milestone | Status | Completed |
|-----------|--------|-----------|
| M1 — Vertical Slice v0.1 | ✅ Done | 2026-01-31 |
| M2 — Beta v0.5 | ✅ Done | 2026-02-01 |
| M3 — Live Reconciliation | ✅ Done | 2026-02-07 |

### Stage D/E E2E Mainnet Verification

| Stage | Status | Description | PR |
|-------|--------|-------------|-----|
| Stage D | ✅ E2E mainnet | `execute_cancel_all` — grinder_ orders cancelled | PR #102 |
| Stage E | ✅ E2E mainnet | `execute_flatten` — position flattened | PR #103 → `9572fd7` |
| Docs | ✅ Runbook-ready | STATE.md updated with tight budgets | PR #104 → `4434284` |

### Key Achievements (LC-* series)

- **LC-12:** `parse_client_order_id()` — fixed `split("_")` bug for cancel routing
- **LC-18:** 5-mode staged rollout (detect_only → plan_only → blocked → execute_cancel_all → execute_flatten)
- **Connector hardening:** Timeouts, retries, idempotency, circuit breaker, connector metrics (see STATE.md)
- **Safety gates verified:**
  - `ALLOW_MAINNET_TRADE=1` hard gate for execute modes
  - `REMEDIATION_*_ALLOWLIST` for strategy + symbol filtering
  - Budget caps: `MAX_CALLS_PER_DAY`, `MAX_NOTIONAL_PER_DAY`
- **Metrics contract:** SSOT in `live_contract.py`, graceful without redis

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

## 3) Planned Milestones (Post-Stage-E)

### M4 — Ops Hardening (P1/P2)
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

### M5 — Observability Polish
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

### M6 — HA / Leader Election (LC-20)
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

---

## 5) Definition of Done (DoD) — Historical Reference

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
- `python3 scripts/check_unicode.py --all`
