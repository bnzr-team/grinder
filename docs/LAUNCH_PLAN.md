# Launch Plan v1 — Grinder Single-Venue (Binance USDT-M Futures)

> **SSOT** for "when is this product launched?" and "what remains?"
>
> Last updated: 2026-02-28
> Main: `158b470` (after TRD-3b, PR #305)

---

## 1) Definition of Done — Launch v1

Launch v1 = **first sustained ACTIVE window on mainnet with real capital at risk**.

Every item below must be PASS (measurable, not "in general ready").

### D1 — Mainnet safe-by-default (3-layer dry-run)

- **How to verify:** `pytest tests/unit/test_safety_envelope.py`
- **Evidence:** PR [#301](https://github.com/bnzr-team/grinder/pull/301) @ `117f45e`
- **SSOT doc:** `docs/20_SAFETY_ENVELOPE.md`
- **Status:** DONE

### D2 — Policy contract locked (GridPolicy + GridPlan)

- **How to verify:** `pytest tests/unit/test_policy_contract.py`
- **Evidence:** PR [#302](https://github.com/bnzr-team/grinder/pull/302) @ `aacca7f`
- **SSOT doc:** `docs/22_POLICY_CONTRACT.md`
- **Status:** DONE

### D3 — Volatility SSOT (natr_bps encoding)

- **How to verify:** `pytest tests/unit/test_natr_contract.py`
- **Evidence:** PR [#304](https://github.com/bnzr-team/grinder/pull/304) @ `92dd4f5`
- **SSOT doc:** `docs/23_NATR_CONTRACT.md`, ADR-078
- **Status:** DONE

### D4 — Volatility→spacing hook locked

- **How to verify:** `pytest tests/unit/test_adaptive_policy_natr_hook_contract.py`
- **Evidence:** PR [#305](https://github.com/bnzr-team/grinder/pull/305) @ `158b470`
- **SSOT doc:** `docs/24_NATR_SPACING_HOOK.md`
- **Status:** DONE

### D5 — OPS runbooks + triage bundles

- **How to verify:** `ls docs/runbooks/*.md | wc -l` (32 runbooks) + `bash scripts/triage_bundle.sh --help`
- **Evidence:** PR [#280](https://github.com/bnzr-team/grinder/pull/280) @ `736abad` (triage_bundle.sh), PR [#300](https://github.com/bnzr-team/grinder/pull/300) @ `498bafe` (preview guard + manifest)
- **SSOT doc:** `docs/runbooks/README.md`
- **Status:** DONE

### D6 — OBS alerts + SLO registry + alert index

- **How to verify:** `python -m scripts.verify_alert_rules monitoring/alert_rules.yml` + `python -m scripts.verify_alert_index docs/runbooks/ALERT_INDEX.md monitoring/alert_rules.yml`
- **Evidence:** PR [#291](https://github.com/bnzr-team/grinder/pull/291) @ `0125a1f` (SLO registry), PR [#295](https://github.com/bnzr-team/grinder/pull/295) @ `cbf7603` (alert contract enforcement), PR [#298](https://github.com/bnzr-team/grinder/pull/298) @ `c1d3b5b` (alert index guard)
- **SSOT doc:** `docs/OBSERVABILITY_SLOS.md`, `docs/runbooks/ALERT_INDEX.md`
- **Status:** DONE

### D7 — CI smoke gate (3 jobs)

- **How to verify:** CI workflow `smoke_gate.yml` runs 3 jobs on every PR
- **Evidence:** PR [#267](https://github.com/bnzr-team/grinder/pull/267) @ `95f23d0`
- **SSOT doc:** `.github/workflows/smoke_gate.yml`
- **Status:** DONE

### D8 — Canary criteria documented

- **How to verify:** `docs/runbooks/32_MAINNET_ROLLOUT_FILL_PROB.md` has "Canary by Symbol" + decision tree
- **Evidence:** PR [#247](https://github.com/bnzr-team/grinder/pull/247) @ `97caebc` (runbook section), PR [#248](https://github.com/bnzr-team/grinder/pull/248) @ `c6029ef` (allowlist code), PR [#249](https://github.com/bnzr-team/grinder/pull/249) @ `3eae60d` (decision tree)
- **Status:** DONE

### D9 — Fill-prob model + controlled rollout

- **How to verify:** `pytest tests/unit/test_fill_model*.py tests/unit/test_router_fill_prob*.py`
- **Evidence:** Track C chain: PR [#232](https://github.com/bnzr-team/grinder/pull/232) @ `3901e61` (dataset) through PR [#245](https://github.com/bnzr-team/grinder/pull/245) @ `4d08b3b` (auto-threshold ceremony)
- **SSOT doc:** `docs/runbooks/31_FILL_PROB_ROLLOUT.md`
- **Status:** DONE

### D10 — Production trading loop (HA-gated)

- **How to verify:** `python3 scripts/run_trading.py --help` shows `--armed`, `--exchange-port`, `--mainnet`
- **Evidence:** PR [#252](https://github.com/bnzr-team/grinder/pull/252) @ `3747281` (entrypoint), PR [#255](https://github.com/bnzr-team/grinder/pull/255) @ `478555c` (HA-gated + selectable port)
- **Status:** DONE

### D11 — Graceful shutdown (no Task-destroyed)

- **How to verify:** `bash scripts/smoke_no_task_destroyed.sh` (CI: smoke-clean-shutdown job)
- **Evidence:** PR [#256](https://github.com/bnzr-team/grinder/pull/256) @ `e32e925` (shutdown + metrics), PR [#258](https://github.com/bnzr-team/grinder/pull/258) @ `34b8eee` (fixture runs)
- **Status:** DONE

### D12 — Alerting pack (3 critical alerts)

- **How to verify:** `grep -c 'alert:' monitoring/alert_rules.yml` shows 53 alerts including EngineInitDown, FillProbBlocksSpike, ReadyzNotReady
- **Evidence:** PR [#257](https://github.com/bnzr-team/grinder/pull/257) @ `6380650` (initial pack), PR [#260](https://github.com/bnzr-team/grinder/pull/260) @ `9dd29a4` (FillProbBlocksHigh + polish)
- **Status:** DONE

### D13 — Fixture network airgap

- **How to verify:** `pytest tests/unit/test_fixture_guard.py`
- **Evidence:** PR [#266](https://github.com/bnzr-team/grinder/pull/266) @ `856f589`
- **SSOT doc:** ADR-075 in `docs/DECISIONS.md`
- **Status:** DONE

### D14 — Runbook 32 ceremony (read_only + live_trade rehearsal)

- **How to verify:** Operator ceremony, not automated. Artifacts in session transcripts.
- **Evidence:** Runbook 32 read_only Phase 0–5 COMPLETE. Runbook 32 live_trade+armed Phase 2–5 COMPLETE. blocks_total=5→10→10→10, cb_trips=0 across all phases. NoOpExchangePort (zero real orders).
- **SSOT doc:** `docs/runbooks/32_MAINNET_ROLLOUT_FILL_PROB.md`
- **Status:** DONE

### D15 — Budget/risk limits verified in smoke

- **How to verify:** `python3 scripts/run_trading.py --fixture ... --paper-size-per-level 0.001` with drawdown + kill-switch
- **Evidence:** PR [#254](https://github.com/bnzr-team/grinder/pull/254) @ `c6e6b40` (rehearsal knobs)
- **Status:** DONE

**Result: 15/15 DONE.** Every criterion has a PR link + commit hash as evidence.

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
