# ROADMAP — Grinder

This file tracks **plan + progress**.

- **Source of truth for current implementation:** `docs/STATE.md`
- **Why key choices were made:** `docs/DECISIONS.md`
- Specs in `docs/*` describe **target behavior** unless `STATE.md` says implemented.

Last updated: 2026-01-31

---

## 1) Current status (from STATE.md)

### Foundation / Infrastructure — Done
- ✅ PR #2–#9: infrastructure fixes (CLI entrypoints, soak runner, docker/grafana, docs truth, proof guards)
- ✅ PR #10: `STATE.md` updated to match reality
- ✅ PR #11: CODEOWNERS added for critical paths

### Now
- ⬜ M1 — Vertical Slice v0.1 (Replay-first)

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
- ⬜ PR-016: GridPolicy v0 (static symmetric grid)
- ⬜ PR-017: Execution stub v0 (apply intents, no exchange)
- ⬜ PR-018: CLI wiring for one end-to-end replay run

---

### M2 — Beta v0.5
**Goal:** paper loop with gating + observability maturity

- ✅ Adaptive Controller spec (docs-only): `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` — merged 2026-01-31
- ⬜ Adaptive Controller implementation (regime + step + reset)
- ⬜ Top-K prefilter working from fixtures/live data
- ⬜ Toxicity gating enabled (`docs/06_TOXICITY_SPEC.md`)
- ⬜ Paper loop validated (no exchange writes)
- ⬜ Backtest protocol applied to at least 2 fixtures
- ⬜ Dashboards and alerts usable for daily ops

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

### PR-015 — GridPolicy v0 (static symmetric grid)
**DoD**
- Implement one minimal policy that produces deterministic intents from a snapshot
- Unit tests for levels/spacing/edge cases
- Update `STATE.md` (policy implemented, parameters + limitations)

**Proof**
- `pytest`, `mypy`
- `verify_replay_determinism` if it affects replay output

---

### PR-016 — Execution stub v0 (apply intents)
**DoD**
- Executor applies intents in replay/paper mode without exchange writes
- Structured logs + basic metrics updated (as per `13_OBSERVABILITY.md` minimal set)
- Unit tests for mapping intents→actions
- Update `STATE.md`

**Proof**
- `pytest`, `mypy`
- `verify_replay_determinism`

---

### PR-017 — CLI wiring end-to-end (replay-first)
**DoD**
- `grinder replay ...` runs end-to-end on a fixture and produces deterministic output
- `verify_replay_determinism` passes on at least one fixture
- Update `STATE.md` (end-to-end slice "implemented")

**Proof**
- `pytest`, `mypy`
- `python -m scripts.verify_replay_determinism --fixture ...` (stable digest)
