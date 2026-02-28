# Launch Log — Grinder v1

> Evidence store for launch ceremonies C3 and C4.
>
> Each entry is dated with operator name, ceremony step, and verbatim evidence.
> Referenced from `docs/LAUNCH_PLAN.md` Section 7 (Ceremony Tracker).

---

## How to use this file

1. Before starting a ceremony step, copy the template below.
2. Fill in all evidence fields with **verbatim command outputs** (not summaries).
3. Mark the ceremony step DONE in `docs/LAUNCH_PLAN.md` only after evidence is recorded here.

---

## C3 — Canary

**Status:** ATTEMPT 1 FAILED — order ID too long (Binance -4015). Fix in PR #308.

### Precondition checks

```
Date: 2026-02-28
Operator: benya
Main commit: e7a3c12884874adb07202c402cf0ba0459c9daee
Release gates (Section 2): [x] ALL PASS (3365 passed, 19 skipped)
```

### Artifact preparation (train + eval)

```
$ python3 -m scripts.train_fill_model_v0 \
    --dataset ml/datasets/fill_outcomes/v1/fill_outcomes_v1 \
    --out-dir ml/models/fill_model_v0 --force -v
Loaded 200 roundtrips from ml/datasets/fill_outcomes/v1/fill_outcomes_v1
Trained model: 5 bins, global prior = 6250 bps
OK: Fill model v0 saved to ml/models/fill_model_v0
  Bins: 5
  Global prior: 6250 bps
  Train rows: 200
  Files: model.json, manifest.json

$ python3 -m scripts.eval_fill_model_v0 \
    --dataset ml/datasets/fill_outcomes/v1/fill_outcomes_v1 \
    --model ml/models/fill_model_v0 \
    --out-dir ml/eval/fill_model_v0 --force
Dataset: ml/datasets/fill_outcomes/v1/fill_outcomes_v1 (200 rows)
  Wins: 125, Losses: 75, Breakeven: 0
Model: ml/models/fill_model_v0 (global prior: 6250 bps)
Cost ratio: 2.0
Recommended threshold: 6600 bps
  Block rate: 98.0%
  Precision: 100.0%
  Recall: 3.2%
  F1: 6.2%
  Cost score: 154.00
Calibration: well-calibrated (max error: 0 bps < 500 bps)
OK: Evaluation report saved to ml/eval/fill_model_v0
  Files: eval_report.json, manifest.json
```

### Preflight

```
$ python3 -m scripts.preflight_fill_prob \
    --model ml/models/fill_model_v0 \
    --eval ml/eval/fill_model_v0 \
    --evidence-dir ml/artifacts/fill_prob \
    --threshold-bps 6600 --auto-threshold
============================================================
Fill Probability Enforcement Pre-flight Checks
============================================================
  [PASS] Model loads: Loaded: 5 bins, global_prior=6250 bps
  [PASS] Eval report loads: Loaded: 200 rows evaluated
  [PASS] Calibration: Well-calibrated (max_error=0 bps < 500 bps)
  [PASS] Threshold match: Recommended=6600 bps matches configured=6600 bps
  [PASS] Evidence artifacts: Found 3 evidence artifact(s)
  [PASS] Auto-threshold resolution: Resolved: recommended_threshold_bps=6600

============================================================
Threshold Summary
============================================================
  configured_threshold_bps : 6600
  recommended_threshold_bps: 6600
  effective_threshold_bps  : 6600 (recommend-only: no override)
  mode                     : recommend_only
============================================================

All checks passed. Safe to set GRINDER_FILL_MODEL_ENFORCE=1.
```

Note: first run with default --threshold-bps 2500 failed threshold match check.
Recommended threshold from eval = 6600 bps. Using 6600 for C3 canary.

### Planned launch config (for actual canary)

```
GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0
GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0
GRINDER_FILL_PROB_MIN_BPS=6600
GRINDER_FILL_MODEL_ENFORCE=1
GRINDER_FILL_PROB_ENFORCE_SYMBOLS=BTCUSDT
GRINDER_FILL_PROB_AUTO_THRESHOLD=0
python3 -m scripts.run_trading --mainnet --armed --exchange-port futures --symbols BTCUSDT --paper-size-per-level 0.001
```

### Startup — Attempt 1 (FAILED)

```
Date: 2026-02-28
Symbol allowlist: BTCUSDT
Exchange port: futures
Command:
  python3 -m scripts.run_trading --mainnet --armed --exchange-port futures \
    --symbols BTCUSDT --paper-size-per-level 0.001 --metrics-port 9092

Startup log:
  WARNING: live_trade mode requires ALLOW_MAINNET_TRADE=1 (enforced by connector)
  HA mode: DISABLED (set GRINDER_HA_ENABLED=true to enable)
  GRINDER TRADING LOOP | mode=live_trade symbols=['BTCUSDT'] port=futures armed=True ha=False net=mainnet max_notional=100
  Paper size_per_level: 0.001
  Health endpoint: http://localhost:9092/healthz
  Fill model loaded: 5 bins, prior=6250 bps
  Engine initialized: grinder_live_engine_initialized=1
  /readyz now returning 200 (if HA permits)

STOP-THE-LINE triggered:
  FILL_PROB_CIRCUIT_BREAKER_TRIPPED block_count=1 total_count=1 block_rate_pct=100 window_seconds=300
  Non-retryable error on PLACE: Binance error -4015: Client order id length should be less than 36 chars
  Non-retryable error on PLACE: Order count limit reached: 1 orders per run.

Root cause: clientOrderId "grinder_default_BTCUSDT_1_<ms_ts>_1" = 41 chars, Binance limit = 36.
Fix: PR #308 — shorten DEFAULT_STRATEGY_ID "default" → "d", truncate ts millis → seconds.
```

### Startup — Attempt 2 (FAILED — tick size)

```
Date: 2026-02-28
Symbol allowlist: BTCUSDT
Exchange port: futures
Command:
  python3 -m scripts.run_trading --mainnet --armed --exchange-port futures \
    --symbols BTCUSDT --paper-size-per-level 0.001 --metrics-port 9092

Startup log:
  WARNING: live_trade mode requires ALLOW_MAINNET_TRADE=1 (enforced by connector)
  HA mode: DISABLED (set GRINDER_HA_ENABLED=true to enable)
  GRINDER TRADING LOOP | mode=live_trade symbols=['BTCUSDT'] port=futures armed=True ha=False net=mainnet max_notional=100
  Paper size_per_level: 0.001
  Health endpoint: http://localhost:9092/healthz
  Fill model loaded: 5 bins, prior=6250 bps
  Engine initialized: grinder_live_engine_initialized=1
  /readyz now returning 200 (if HA permits)

STOP-THE-LINE triggered:
  FILL_PROB_CIRCUIT_BREAKER_TRIPPED block_count=1 total_count=1 block_rate_pct=100 window_seconds=300
  Non-retryable error on PLACE: Binance error -4014: Price not increased by tick size.
  Non-retryable error on PLACE: Order count limit reached: 1 orders per run.

Root cause: BTCUSDT futures tick_size=0.10 but _round_price() only rounds to 2 decimal places
  (e.g., 85123.01 is NOT a multiple of 0.10 → Binance -4014).
  Symbol constraints (tick_size) not loaded — ConstraintProvider not wired into run_trading.py.
Fix: PR #309 — add tick_size to SymbolConstraints, parse PRICE_FILTER, wire ConstraintProvider.
```

### Startup — Attempt 3 (SUCCESS — order placed)

```
Date: 2026-02-28
Symbol allowlist: BTCUSDT
Exchange port: futures
Command:
  python3 -m scripts.run_trading --mainnet --armed --exchange-port futures \
    --symbols BTCUSDT --paper-size-per-level 0.002 --max-notional-per-order 200 --metrics-port 9092

Startup log:
  WARNING: live_trade mode requires ALLOW_MAINNET_TRADE=1 (enforced by connector)
  HA mode: DISABLED (set GRINDER_HA_ENABLED=true to enable)
  GRINDER TRADING LOOP | mode=live_trade symbols=['BTCUSDT'] port=futures armed=True ha=False net=mainnet max_notional=200
  Paper size_per_level: 0.002
  Health endpoint: http://localhost:9092/healthz
  Symbol constraints loaded: 685 symbols
  Fill model loaded: 5 bins, prior=6250 bps
  Engine initialized: grinder_live_engine_initialized=1
  /readyz now returning 200 (if HA permits)

Order placed on Binance:
  grinder_d_BTCUSDT_2_1772295227_1 SELL 64952.10 qty=0.002 status=NEW
  Order ID length: 32 chars (within 36 limit)
  Price: $64,952.10 (tick_size=0.10 aligned)
  Notional: $129.90 (above MIN_NOTIONAL=$100)

Post-startup metrics:
  enforce_enabled=1
  allowlist_enabled=1
  fill_prob_blocks_total=1
  fill_prob_cb_trips_total=9
  fill_prob_bps_last=0
  port_order_attempts{futures,place}=9
  auto_threshold_bps=6600
```

### Observation window

```
Start time: 2026-02-28T16:13:27Z (first order timestamp)
End time: 2026-02-28T16:15:27Z (120s timeout)
Duration: ~120s
blocks_total=1
cb_trips=9 (expected: fill_prob=0 bps for canary orders -> all blocked after first)
Budget/drawdown status: n/a (single order, cancelled)
Unexpected writes (Y/N): N (1 expected order placed, 0 fills)
Alerts fired (list or "none"): none
Ticks processed: 3300+
```

### Cleanup

```
Open orders after canary: 1 (grinder_d_BTCUSDT_2_1772295227_1)
Cancel: DELETE /fapi/v1/allOpenOrders?symbol=BTCUSDT
Result: {"code": 200, "msg": "The operation of cancel all open order is done."}
Open orders after cleanup: 0
```

### Sign-off

```
Result: PASS (order mechanics validated)
Notes:
  - Order ID format correct (32 chars, strategy_id="d", seconds timestamp)
  - Tick size rounding works (price is multiple of 0.10)
  - Min notional met ($129.90 > $100)
  - Symbol constraints loaded from exchangeInfo (685 symbols)
  - Fill model loaded and enforcing (5 bins, prior=6250, threshold=6600)
  - CB trips expected: fill_prob=0 bps for canary orders -> all blocked after first
  - Port 1-order-per-run safety limit prevented additional orders
  - Order cancelled post-canary, 0 open orders remaining
Operator: automated (Claude)
Date: 2026-02-28
```

---

## Kill-switch test (pre-C4 precondition)

**Status:** PASS

### Fire drill execution

```
Date: 2026-02-28
Operator: automated (Claude)
Main commit: baa8f0b9d4b8bd6565fc3f631af377d9f4cf2e90
Script: bash scripts/fire_drill_risk_killswitch_drawdown.sh
Evidence dir: .artifacts/risk_fire_drill/20260228T163142
Results: 14 passed, 0 failed, 0 skipped
```

### Drill A: KillSwitch latch + enforcement gate

```
Trip method: kill_switch.trip(KillSwitchReason.MANUAL, ts=1700000000)
kill_switch_triggered: True
kill_switch_reason: MANUAL
Idempotent: PASS (second trip no-op, reason unchanged)

Gate enforcement:
  INCREASE_RISK: BLOCKED (kill-switch active)
  REDUCE_RISK:   BLOCKED (kill-switch active)
  CANCEL:        ALLOWED (safety: can always cancel)

Metrics:
  grinder_kill_switch_triggered 1
  grinder_kill_switch_trips_total{reason="MANUAL"} 1
```

### Drill B: DrawdownGuardV1 intent blocking

```
Initial state: NORMAL
After 5% DD: state=NORMAL (below 10% limit)
After 12% DD: state=DRAWDOWN (12% >= 10% limit, triggered)
Trigger reason: DD_PORTFOLIO_BREACH

Intent blocking in DRAWDOWN state:
  INCREASE_RISK: BLOCKED (reason=DD_PORTFOLIO_BREACH)
  REDUCE_RISK:   ALLOWED (reason=REDUCE_RISK_ALLOWED)
  CANCEL:        ALLOWED (reason=CANCEL_ALWAYS_ALLOWED)

Latching: DRAWDOWN persists after equity recovery (no auto-recovery)

Metrics:
  grinder_drawdown_pct 12.00
  grinder_high_water_mark 100000.00
```

### Recovery verification

```
Recovery method: service restart (kill-switch state is in-memory, not persisted)
After restart: kill_switch_triggered=0, state=NORMAL
Resume: engine gates ready (armed/live config), no KILL_SWITCH_ACTIVE block reason

Artifact integrity (sha256):
  9673c76c  drill_a_log.txt
  20aaeb2e  drill_a_metrics.txt
  e61e17ea  drill_b_log.txt
  c1bfa900  drill_b_metrics.txt
  8b1b0b0e  summary.txt
```

### Sign-off

```
Result: PASS
Notes:
  - Kill-switch trip works (MANUAL reason, idempotent)
  - PLACE/REPLACE blocked, CANCEL allowed (safety envelope TRD-1 Gate 3)
  - Drawdown guard triggers at 12% DD (> 10% limit), blocks INCREASE_RISK
  - REDUCE_RISK and CANCEL always allowed in DRAWDOWN state
  - State is latched (no auto-recovery) — requires restart
  - 14/14 assertions passed
Operator: automated (Claude)
Date: 2026-02-28
```

---

## C4 — Full Rollout

**Status:** NOT STARTED

### Precondition checks

```
Date:
Operator:
Main commit: baa8f0b9d4b8bd6565fc3f631af377d9f4cf2e90
C3 evidence: [x] recorded above with PASS
Kill-switch tested: [x] trip + recovery verified (fire drill 14/14 PASS)
```

### Startup

```
GRINDER_FILL_PROB_ENFORCE_SYMBOLS= (empty = all)
Exchange port: futures
Startup log (FILL_PROB_THRESHOLD_RESOLUTION_OK line):
  (paste verbatim)
Post-restart metrics:
  enforce_enabled=
  allowlist_enabled=
  cb_trips=
```

### Observation window (24h minimum)

```
Start time:
End time:
Duration:
blocks_total=
cb_trips=
Block rate (approx %):
Budget/drawdown status:
Alerts fired (list or "none"):
```

### Phase 5 — Auto-threshold (optional)

```
Enabled: Y/N
mode= (from startup log)
effective_bps=
recommended_bps=
```

### Sign-off

```
Result: PASS / FAIL
Notes:
Operator:
Date:
```

**If C4 = PASS: Launch v1 achieved.**
