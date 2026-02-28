# Launch Plan v1 — Grinder Single-Venue (Binance USDT-M Futures)

> **SSOT** for "when is this product launched?" and "what remains?"
>
> Last updated: 2026-02-28
> Main: `158b470` (after TRD-3b, PR #305)

---

## 1) Definition of Done — Launch v1

Launch v1 = **first sustained ACTIVE window on mainnet with real capital at risk**.

Every item below must be PASS (measurable, not "in general ready").

| # | Criterion | How to verify | Status |
|---|-----------|---------------|--------|
| D1 | Mainnet safe-by-default (3-layer dry-run) | TRD-1 contract tests: `pytest tests/unit/test_safety_envelope.py` | DONE (PR #301) |
| D2 | Policy contract locked (GridPolicy + GridPlan) | TRD-2 contract tests: `pytest tests/unit/test_policy_contract.py` | DONE (PR #302) |
| D3 | Volatility SSOT (natr_bps encoding) | TRD-3a contract tests: `pytest tests/unit/test_natr_contract.py` | DONE (PR #304) |
| D4 | Volatility→spacing hook locked | TRD-3b contract tests: `pytest tests/unit/test_adaptive_policy_natr_hook_contract.py` | DONE (PR #305) |
| D5 | OPS runbooks + triage bundles | `ls docs/runbooks/*.md` (32 runbooks) + `scripts/triage_bundle.sh` | DONE (PR #280–#300) |
| D6 | OBS alerts + SLO registry + alert index | `python -m scripts.verify_alert_rules monitoring/alert_rules.yml` + `python -m scripts.verify_alert_index docs/runbooks/ALERT_INDEX.md monitoring/alert_rules.yml` | DONE (PR #291–#298) |
| D7 | CI smoke gate (3 jobs) | smoke-clean-shutdown + smoke-ha-metrics + smoke-futures-no-orders | DONE (PR #267) |
| D8 | Canary criteria documented | Runbook 32 + canary decision tree | DONE (PR #247–#249) |
| D9 | Fill-prob model + controlled rollout | CB + preflight + auto-threshold + SOR gate | DONE (Track C: PR #232–#245) |
| D10 | Production trading loop (HA-gated) | `scripts/run_trading.py` with `--armed --exchange-port futures` | DONE (PR #252–#255) |
| D11 | Graceful shutdown (no Task-destroyed) | smoke_no_task_destroyed.sh in CI | DONE (PR #256, #258) |
| D12 | Alerting pack (3 critical alerts) | EngineInitDown + FillProbBlocksSpike + ReadyzNotReady in alert_rules.yml | DONE (PR #257, #259–#260) |
| D13 | Fixture network airgap | Socket-level block in `--fixture` mode | DONE (PR #266) |
| D14 | Runbook 32 ceremony (read_only + live_trade rehearsal) | Phase 0–5 COMPLETE on NoOpExchangePort | DONE |
| D15 | Budget/risk limits verified in smoke | `--paper-size-per-level` + drawdown guard + kill-switch | DONE (PR #254) |

**Result: 15/15 DONE.** All measurable criteria met.

---

## 2) Release Gates (commands that must PASS before GO)

Every command must exit 0. Run from repo root (`/home/benya/Project/grinder`).

### CI-equivalent gates (automated)

```bash
# 1. Lint
ruff check .
ruff format --check .

# 2. Unicode security
python3 scripts/check_unicode.py

# 3. Alert rules contract
python3 -m scripts.verify_alert_rules monitoring/alert_rules.yml
python3 -m scripts.verify_alert_index docs/runbooks/ALERT_INDEX.md monitoring/alert_rules.yml

# 4. Type checking
python3 -m mypy .

# 5. Full test suite
python3 -m pytest -q

# 6. Determinism suite
python3 -m scripts.verify_determinism_suite

# 7. Backtest replay
python3 -m scripts.run_replay --fixture tests/fixtures/sample_day/ -v
```

### Smoke gates (CI: smoke_gate.yml)

```bash
# 8. Clean shutdown (no "Task was destroyed")
bash scripts/smoke_no_task_destroyed.sh

# 9. HA metrics invariants
bash scripts/smoke_ha_metrics_invariants.sh

# 10. Futures no-orders (fixture mode)
bash scripts/smoke_futures_no_orders.sh
```

### Operator ceremony gates (manual, pre-ACTIVE)

```bash
# 11. Runbook 32 — read_only rehearsal
#     See docs/runbooks/32_MAINNET_ROLLOUT_FILL_PROB.md Phase 0–2

# 12. Runbook 32 — live_trade + armed rehearsal (NoOpExchangePort)
#     See docs/runbooks/32_MAINNET_ROLLOUT_FILL_PROB.md Phase 3–5

# 13. Canary-by-symbol (single symbol, small budget)
#     See docs/runbooks/32_MAINNET_ROLLOUT_FILL_PROB.md "Canary by Symbol"
```

---

## 3) Remaining Work — Finite List

### Pre-launch (blocking GO)

| ID | Title | Scope | Effort | Status | PR |
|----|-------|-------|--------|--------|-----|
| — | — | — | — | — | — |

**No remaining pre-launch PRs.** All D1–D15 criteria are met.

### Pre-ACTIVE ceremony (not code, operator actions)

| # | Step | Runbook | Status |
|---|------|---------|--------|
| C1 | Runbook 32 read_only rehearsal (NoOp) | 32_MAINNET_ROLLOUT Phase 0–2 | DONE |
| C2 | Runbook 32 live_trade+armed rehearsal (NoOp) | 32_MAINNET_ROLLOUT Phase 3–5 | DONE |
| C3 | Runbook 32 canary (1 symbol, real BinanceFuturesPort) | 32_MAINNET_ROLLOUT "Canary by Symbol" | TODO |
| C4 | Runbook 32 full rollout (all symbols, ACTIVE) | 32_MAINNET_ROLLOUT "Full Rollout" | TODO |

**C3 is the next action.** Everything before it is code-complete.

### Post-launch (not blocking GO — tracked in POST_LAUNCH_ROADMAP.md)

All post-launch work lives in `docs/POST_LAUNCH_ROADMAP.md` § 3 (P2 Backlog, 12 gaps).
It is explicitly **out of scope** for Launch v1.

---

## 4) Timeline — Effort Remaining

| Bucket | Count | Effort | Notes |
|--------|-------|--------|-------|
| Pre-launch PRs | 0 | — | All code shipped |
| Pre-ACTIVE ceremony steps | 2 (C3, C4) | Operator time | Requires API credentials + mainnet access |
| Post-launch P2 backlog | 12 gaps | Unbounded | Tracked separately in POST_LAUNCH_ROADMAP.md |

**Code is launch-ready.** The remaining work is operational:
- **C3 (canary):** Single-symbol real-money test with tight budget. Requires operator with API keys.
- **C4 (full rollout):** Multi-symbol ACTIVE window after canary validates.

---

## 5) SSOT Links

| Document | Purpose |
|----------|---------|
| `docs/STATE.md` | What actually works now (implementation truth) |
| `docs/GAPS.md` | Spec vs implementation delta |
| `docs/POST_LAUNCH_ROADMAP.md` | P2 backlog (post-launch) |
| `docs/OBSERVABILITY_SLOS.md` | SLO registry (5 primary + 5 related) |
| `docs/runbooks/06_ALERT_RESPONSE.md` | First-60s alert response |
| `docs/runbooks/ALERT_INDEX.md` | Alert → runbook routing |
| `docs/runbooks/32_MAINNET_ROLLOUT_FILL_PROB.md` | Mainnet rollout ceremony |
| `docs/20_SAFETY_ENVELOPE.md` | TRD-1: gate ordering contract |
| `docs/22_POLICY_CONTRACT.md` | TRD-2: GridPolicy/GridPlan lockdown |
| `docs/23_NATR_CONTRACT.md` | TRD-3a: natr_bps encoding SSOT |
| `docs/24_NATR_SPACING_HOOK.md` | TRD-3b: compute_step_bps formula |
| `docs/DECISIONS.md` | ADR index (78 decisions) |

---

## 6) Stop-the-Line Rules

Any of these **blocks GO** until resolved:

1. **CI red on main** — any of the 10 required checks failing.
2. **Contract test regression** — TRD-1/2/3a/3b tests failing means safety envelope broken.
3. **Determinism drift** — `verify_determinism_suite` exit != 0.
4. **Kill-switch not tested** — must verify kill-switch trip + recovery before each ACTIVE window.
5. **Budget exhausted** — daily notional/call limits hit → stop trading, not increase limits.
6. **Canary failure** — if C3 canary shows unexpected behavior, DO NOT proceed to C4.

---

## Changelog

| Date | Change |
|------|--------|
| 2026-02-28 | Initial version. All D1–D15 met. C1–C2 ceremonies done. |
