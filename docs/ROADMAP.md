# ROADMAP — Grinder

This file tracks **plan + progress**.

- **Source of truth for current implementation:** `docs/STATE.md`
- **Why key choices were made:** `docs/DECISIONS.md`
- Specs in `docs/*` describe **target behavior** unless `STATE.md` says implemented.

Last updated: 2026-02-01

---

## 1) Current status (from STATE.md)

### Foundation / Infrastructure — Done
- ✅ PR #2–#9: infrastructure fixes (CLI entrypoints, soak runner, docker/grafana, docs truth, proof guards)
- ✅ PR #10: `STATE.md` updated to match reality
- ✅ PR #11: CODEOWNERS added for critical paths

### Now
- ✅ M1 — Vertical Slice v0.1 (Replay-first) — completed 2026-01-31
- ✅ M2 — Beta v0.5 (paper loop + gating) — completed 2026-02-01
- ⬜ M3 — Production v1.0 (hardening + ops)

---

## 2) Milestones

### M1 — Vertical Slice v0.1 (Replay-first)
**Goal:** fixture → prefilter → policy → order intents → execution stub → metrics/logs → deterministic replay

**Governing docs**
- `docs/11_BACKTEST_PROTOCOL.md` (determinism rules)
- `docs/04_PREFILTER_SPEC.md` (gating intent)
- `docs/07_GRID_POLICY_LIBRARY.md` (policy intent)
- `docs/09_EXECUTION_SPEC.md` (intent→execution contract)
- `docs/10_RISK_SPEC.md` (limits/kill-switch expectations)
- `docs/13_OBSERVABILITY.md` (metrics/logging expectations)

**Success criteria (measurable)**
- `python -m scripts.verify_replay_determinism --fixture <...>` returns stable digest for the same fixture
- Unit tests exist for each stage (prefilter/policy/execution glue)
- CLI can run one end-to-end replay (skeleton mode is fine)
- `STATE.md` updated after each PR to reflect what is implemented

**Work items (recommended PR sequence)**
- ✅ PR-013: Domain contracts (events/state/order_intents) — merged 2026-01-31
- ✅ PR-014: Prefilter v0 (rule-based gating) — merged 2026-01-31
- ✅ PR-015: Adaptive Controller spec + unicode scanner — merged 2026-01-31
- ✅ PR-016: GridPolicy v0 (static symmetric grid) — merged 2026-01-31 (GitHub PR #17)
- ✅ PR-017: Execution stub v0 (apply intents, no exchange) — merged 2026-01-31 (GitHub PR #20)
- ✅ PR-018: CLI wiring for one end-to-end replay run — merged 2026-01-31

---

### M2 — Beta v0.5
**Goal:** paper loop with gating + observability maturity

**Work items (recommended PR sequence)**
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
  - Note: Also fixed `.github/workflows/promtool.yml` (added `--entrypoint promtool`)
- ✅ PR-029: docs/DEV.md (developer environment setup guide) — merged 2026-02-01 (GitHub PR #36)

**M2 DoD achieved:**
- Paper loop with fills, positions, PnL ✓
- Gating: rate limit + risk + toxicity ✓
- Top-K prefilter working on fixtures ✓
- Adaptive Controller v0 (rule-based) ✓
- Backtest protocol on 5 fixtures ✓
- Observability stack with dashboards + alerts ✓

---

### M3 — Production v1.0
**Goal:** hardening + operations + safety
- ⬜ Connector integration hardened (timeouts, retries, idempotency)
- ⬜ Risk controls complete (kill-switch, limits, drawdown guard)
- ⬜ HA deployment + runbooks
- ⬜ Monitoring dashboards + alerts finalized
- ⬜ Soak thresholds used as a release gate

---

## 3) Traceability matrix (milestones → docs → proofs → outputs)

If a PR touches a milestone scope, it must:
- follow the governing specs below,
- include the required Proof Bundle items,
- update `STATE.md`.

| Milestone | Scope / Deliverable | Governing docs | Required proofs/tests | Outputs / artifacts |
|---|---|---|---|---|
| M1 — Vertical Slice v0.1 | End-to-end fixture pipeline: prefilter → policy → intents → execution stub → metrics/logs → deterministic replay | `STATE.md`, `11_BACKTEST_PROTOCOL.md`, `04_PREFILTER_SPEC.md`, `07_GRID_POLICY_LIBRARY.md`, `09_EXECUTION_SPEC.md`, `10_RISK_SPEC.md` | `pytest`, `mypy`, `python -m scripts.verify_replay_determinism` | Replay output JSON + stable digest |
| M2 — Beta v0.5 | Paper loop + gating + observability | `STATE.md`, `13_OBSERVABILITY.md`, `06_TOXICITY_SPEC.md`, `09_EXECUTION_SPEC.md`, `10_RISK_SPEC.md` | `pytest`, `mypy`, docker/compose smoke (`/healthz`, `/metrics`), `python -m scripts.secret_guard --verbose` | Running paper loop + dashboards |
| M3 — Production v1.0 | Ops + safety hardening | `STATE.md`, `10_RISK_SPEC.md`, `09_EXECUTION_SPEC.md`, `13_OBSERVABILITY.md`, `14_GITHUB_WORKFLOW.md`, `DECISIONS.md` | `pytest`, `mypy`, soak thresholds pass, docker/compose smoke, security scans | Runbooks + release gates |

---

## 4) Definition of Done (DoD) for M1 PRs

This is the checklist that must be satisfied for each PR in M1 to be considered "Done".

### PR-013 — Domain contracts (events/state/order_intents)
**DoD**
- Introduce minimal typed contracts used across the pipeline:
  - Snapshot/event type(s), PolicyState, OrderIntent, Decision container
- No business logic yet — only contracts + tests
- Update `STATE.md` ("contracts added")

**Proof**
- `PYTHONPATH=src python -m pytest -q`
- `PYTHONPATH=src python -m mypy .`

---

### PR-014 — Prefilter v0 (rule-based)
**DoD**
- Implement a rule-based gate that returns ALLOW/BLOCK + reason
- Unit tests: allow + block paths
- Update `STATE.md` (prefilter implemented, limitations listed)

**Proof**
- `pytest`, `mypy`
- If fixtures touched: `python -m scripts.verify_replay_determinism ...`

---

### PR-015 — Adaptive Controller spec + unicode scanner
**DoD**
- Add `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` (regime/step/reset spec)
- Add `scripts/check_unicode.py` with `--all` flag for Python files
- Docs-only + tooling, no production code

**Proof**
- `python scripts/check_unicode.py --all` passes
- Spec reviewed and merged

---

### PR-016 — GridPolicy v0 (static symmetric grid)
**DoD**
- Implement one minimal policy that produces deterministic intents from a snapshot
- Unit tests for levels/spacing/edge cases
- Update `STATE.md` (policy implemented, parameters + limitations)

**Proof**
- `pytest`, `mypy`
- `verify_replay_determinism` if it affects replay output

---

### PR-017 — Execution stub v0 (apply intents)
**DoD**
- Executor applies intents in replay/paper mode without exchange writes
- Structured logs + basic metrics updated (as per `13_OBSERVABILITY.md` minimal set)
- Unit tests for mapping intents→actions
- Update `STATE.md`

**Proof**
- `pytest`, `mypy`
- `verify_replay_determinism`

---

### PR-018 — CLI wiring end-to-end (replay-first)
**DoD**
- `grinder replay ...` runs end-to-end on a fixture and produces deterministic output
- `verify_replay_determinism` passes on at least one fixture
- Update `STATE.md` (end-to-end slice "implemented")

**Proof**
- `pytest`, `mypy`
- `python -m scripts.verify_replay_determinism --fixture ...` (stable digest)

---

## 5) Definition of Done (DoD) for M2 PRs

### PR-019 — Paper Loop v0 + Gating
**DoD**
- PaperExecutionPort/PaperEngine: execution without real orders
- Gating v0: at least 2 gates (rate limit + risk limit)
- CLI: `grinder paper --fixture <path>` runs paper trading on fixture
- Unit tests for gates (allow/block paths)
- E2E test for paper loop with deterministic digest
- Update `STATE.md` (paper loop + gating implemented)

**Proof**
- `pip install -e .`
- `grinder --help` + `grinder paper --help`
- `PYTHONPATH=src python3 -m pytest -q`
- `ruff check .` + `ruff format --check .`
- `mypy .`
- `python3 scripts/check_unicode.py --all`

---

### PR-020 — Gating metrics contract
**DoD**
- `GatingMetrics` class with `record_allowed()`, `record_blocked()`, `to_prometheus_lines()`
- `GateName` and `GateReason` enums with stable values for metric labels
- Contract tests ensuring label values don't change
- Update `STATE.md` (gating metrics documented)

**Proof**
- `pytest tests/unit/test_gating_contracts.py`
- Verify metric format: `grinder_gating_allowed_total{gate="..."}`, `grinder_gating_blocked_total{gate="...",reason="..."}`

---

### PR-021 — Observability /metrics endpoint
**DoD**
- `/metrics` endpoint exports system metrics + gating metrics in Prometheus format
- `MetricsBuilder` consolidates all metrics
- Contract tests for metric names and labels
- Update `STATE.md` (observability documented)

**Proof**
- `curl localhost:9090/metrics` shows gating metrics
- `pytest tests/unit/test_observability.py`

---

### PR-022 — Allowed-orders fixture + fill coverage
**DoD**
- New fixture `tests/fixtures/sample_day_allowed/` with events that pass prefilter + gating
- At least 1 order placed (not blocked)
- Canonical digest locked in config.json and tests
- Determinism verified across runs
- Update `STATE.md` and `ROADMAP.md`

**Proof**
- `grinder paper --fixture tests/fixtures/sample_day_allowed` → orders_placed > 0
- Digest matches `f78930356488da3e`
- `pytest tests/unit/test_paper.py::TestAllowedOrdersFixture` passes
