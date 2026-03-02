# Runbook 33: Trading Loop Activation & Rollback

Operator checklist for enabling the `run_trading.py` trading loop with FSM, emergency exit,
account sync, and HA. Covers pre-flight, live monitoring, rollback triggers, and rollback procedure.

**Scope:** This runbook covers the _engine subsystems_ around the trading loop.
For fill-probability enforcement ceremony, see [Runbook 32](32_MAINNET_ROLLOUT_FILL_PROB.md).
For FSM operator override (force PAUSE/EMERGENCY), see [Runbook 27](27_FSM_OPERATOR_OVERRIDE.md).
For kill-switch details, see [Runbook 04](04_KILL_SWITCH.md).

---

## 1. Activation Checklist

### 1.1 Required env vars

Every variable below is consumed in code. `Source:` comments point to the exact file.

```bash
# --- FSM (must be set BEFORE trading mode) ---
export GRINDER_FSM_ENABLED=1
# Source: scripts/run_trading.py:511 ("FSM + guards" block, default=false)
# Enables FSM + DrawdownGuard + ToxicityGate wiring in the entrypoint.
# Without this, the engine runs with NO state machine — no EMERGENCY transitions,
# no drawdown gating, no toxicity gating. Required for C4.

# --- Trading mode ---
export GRINDER_TRADING_MODE=live_trade
# Source: scripts/run_trading.py:203 (SafeMode enum)

export GRINDER_TRADING_LOOP_ACK=YES_I_KNOW
# Source: scripts/run_trading.py:214-216 (required for paper/live_trade)

export ALLOW_MAINNET_TRADE=1
# Source: scripts/run_trading.py:220, src/grinder/execution/binance_futures_port.py:133

# --- Exchange port (futures) ---
export GRINDER_REAL_PORT_ACK=YES_I_REALLY_WANT_MAINNET
# Source: scripts/run_trading.py:241-246 (required for --exchange-port futures)

export BINANCE_API_KEY=<your-key>
export BINANCE_API_SECRET=<your-secret>
# Source: scripts/run_trading.py:253-258 (required for --exchange-port futures)

# --- Emergency exit ---
export GRINDER_EMERGENCY_EXIT_ENABLED=1
# Source: src/grinder/live/engine.py:309 (safe-by-default=false)
# Enables EmergencyExitExecutor: cancel all + MARKET reduce_only on FSM EMERGENCY.
# Required for C4 full rollout.

# --- Account sync ---
export GRINDER_ACCOUNT_SYNC_ENABLED=1
# Source: src/grinder/live/engine.py:269-271 (safe-by-default=false)
# Enables AccountSyncer: position + open order reconciliation.
# Feeds position_notional_usd to FSM for EMERGENCY recovery.

# --- HA (if multi-instance) ---
export GRINDER_HA_ENABLED=true
# Source: scripts/run_trading.py:34 (LeaderElector, ACTIVE-only processing)
# Omit or set false for single-instance deployment.
```

### 1.2 Optional env vars

```bash
# --- FSM operator override (force state) ---
# export GRINDER_OPERATOR_OVERRIDE=PAUSE
# Source: src/grinder/live/engine.py:487 (read every tick)
# See Runbook 27 for full override semantics.

# --- FSM evidence artifacts ---
export GRINDER_FSM_EVIDENCE=1
# Source: src/grinder/live/fsm_evidence.py (writes .txt + .sha256 per transition)

export GRINDER_ARTIFACT_DIR=artifacts/
# Source: used by FSM evidence, fill-prob resolution, threshold artifacts

# --- SOR feature flag ---
export GRINDER_SOR_ENABLED=1
# Source: src/grinder/live/engine.py:268 (safe-by-default=false)

# --- Fill-prob enforcement (see Runbook 32) ---
# GRINDER_FILL_MODEL_ENFORCE, GRINDER_FILL_MODEL_DIR,
# GRINDER_FILL_PROB_EVAL_DIR, GRINDER_FILL_PROB_AUTO_THRESHOLD,
# GRINDER_FILL_PROB_ENFORCE_SYMBOLS, GRINDER_FILL_PROB_MIN_BPS
```

### 1.3 CLI flags

```bash
python3 scripts/run_trading.py \
    --symbols BTCUSDT,ETHUSDT \
    --mainnet \
    --armed \
    --exchange-port futures \
    --max-notional-per-order 100 \
    --max-orders-per-run 1 \
    --metrics-port 9090
```

| Flag | Default | Effect | Source |
|------|---------|--------|--------|
| `--symbols` | `BTCUSDT,ETHUSDT` | Comma-separated symbols | `run_trading.py:591` |
| `--mainnet` | off (testnet) | Use mainnet WS endpoint | `run_trading.py:600-604` |
| `--armed` | off | Arm engine gate chain (lets actions reach fill-prob gate) | `run_trading.py:606-610` |
| `--exchange-port` | `noop` | `noop` or `futures` (BinanceFuturesPort) | `run_trading.py:619-624` |
| `--max-notional-per-order` | `100` | USD cap per order (rehearsal safety) | `run_trading.py:626-630` |
| `--max-orders-per-run` | `1` | Orders per run. >1 requires `GRINDER_MAX_ORDERS_ACK=YES_I_ACCEPT_MULTI_ORDER` | `run_trading.py:632-637` |
| `--fixture` | unset | JSONL fixture path (canned data, no WS) | `run_trading.py:594-598` |
| `--duration-s` | `0` (infinite) | Auto-stop after N seconds | `run_trading.py:592` |
| `--metrics-port` | `9090` | HTTP port for `/healthz`, `/readyz`, `/metrics` | `run_trading.py:593` |

### 1.4 ACK guards summary

| Guard | Env var | Required value | Triggered by |
|-------|---------|----------------|--------------|
| Trading mode | `GRINDER_TRADING_LOOP_ACK` | `YES_I_KNOW` | `GRINDER_TRADING_MODE=paper` or `live_trade` |
| Real port | `GRINDER_REAL_PORT_ACK` | `YES_I_REALLY_WANT_MAINNET` | `--exchange-port futures` |
| Multi-order | `GRINDER_MAX_ORDERS_ACK` | `YES_I_ACCEPT_MULTI_ORDER` | `--max-orders-per-run >1` |

---

## 2. Pre-flight Checks

Run **before** starting the trading loop. All must pass.

```bash
# 1. Verify commit
git rev-parse HEAD
# Record this hash in ceremony log.

# 2. Local gates (optional for docs-only; mandatory if code changed)
ruff check . && ruff format --check . && python3 -m mypy . && python3 -m pytest -q

# 3. Smoke tests
bash scripts/smoke_no_task_destroyed.sh
bash scripts/smoke_ha_metrics_invariants.sh
bash scripts/smoke_futures_no_orders.sh

# 4. Fill-prob preflight (if enforcement enabled)
python3 -m scripts.preflight_fill_prob \
    --model "$GRINDER_FILL_MODEL_DIR" \
    --eval "$GRINDER_FILL_PROB_EVAL_DIR" \
    --auto-threshold
```

---

## 3. Live Monitoring

### 3.1 Metric watchlist

All metrics below are exported at `/metrics` (default port 9090). Every metric name is a
stable contract defined in `src/grinder/observability/metrics_contract.py`.

| Metric | Type | What it tells you | Red flag |
|--------|------|-------------------|----------|
| `grinder_live_engine_initialized` | gauge | Engine started successfully | `0` after startup = init failure |
| `grinder_readyz_ready` | gauge | Loop ready + HA ACTIVE | `0` when expecting `1` |
| `grinder_fsm_current_state{state="..."}` | gauge | Current FSM state (one-hot) | Stuck in `EMERGENCY` or `INIT` |
| `grinder_fsm_transitions_total{from_state,to_state,reason}` | counter | State transitions | Rapid flapping (many transitions/min) |
| `grinder_fsm_state_duration_seconds` | gauge | Time in current state | `EMERGENCY` > 10 min = investigate |
| `grinder_fsm_action_blocked_total{state,intent}` | counter | Actions blocked by FSM | Unexpected blocks in `ACTIVE` |
| `grinder_kill_switch_triggered` | gauge | Kill-switch latch | `1` = **immediate rollback** |
| `grinder_kill_switch_trips_total{reason}` | counter | Kill-switch trip reasons | Any increment = investigate |
| `grinder_drawdown_pct` | gauge | Current drawdown fraction | Approaching `0.20` (default threshold) |
| `grinder_emergency_exit_enabled` | gauge | Emergency exit feature flag | `0` when expecting `1` |
| `grinder_emergency_exit_total{result}` | counter | Exit executions by result | `result="error"` = investigate |
| `grinder_emergency_exit_orders_cancelled_total` | counter | Orders cancelled by exit | Unexpected increment |
| `grinder_emergency_exit_positions_closed_total` | counter | Positions closed by exit | Unexpected increment |
| `grinder_account_sync_last_ts` | gauge | Last successful sync timestamp | Stale (>120s old) |
| `grinder_account_sync_age_seconds` | gauge | Seconds since last sync | >120 = sync stalled (fires `AccountSyncStale`) |
| `grinder_account_sync_errors_total{reason}` | counter | Sync errors | Sustained increment |
| `grinder_account_sync_mismatches_total{rule}` | counter | Position/order mismatches | Any increment = check exchange |
| `grinder_account_sync_positions_count` | gauge | Open positions count | Unexpected increase |
| `grinder_account_sync_open_orders_count` | gauge | Open orders on exchange | Unexpected increase |
| `grinder_ha_role{role="..."}` | gauge | HA role (one-hot) | Not `ACTIVE` when expected |
| `grinder_ha_is_leader` | gauge | Leader status | `0` when single-instance |

### 3.2 Quick monitoring one-liner

```bash
watch -n 5 'curl -sf http://localhost:9090/metrics | grep -E \
  "kill_switch_triggered |drawdown_pct |fsm_current_state|emergency_exit_total|account_sync_age|readyz_ready "'
```

### 3.3 Health endpoints

| Endpoint | Success | Meaning |
|----------|---------|---------|
| `GET /healthz` | 200 `{"status": "ok", ...}` | Process alive |
| `GET /readyz` | 200 `{"ready": true, ...}` | Loop ready + HA active |
| `GET /readyz` | 503 `{"ready": false, ...}` | Not ready or HA standby |
| `GET /metrics` | 200 | Prometheus text format |

---

## 4. Rollback Trigger Criteria

**Any of these = initiate rollback (Section 5) immediately.**

| # | Condition | How to detect | Threshold |
|---|-----------|---------------|-----------|
| R1 | Kill-switch tripped | `grinder_kill_switch_triggered == 1` | Any trip |
| R2 | Drawdown approaching limit | `grinder_drawdown_pct >= 0.15` | 75% of default 0.20 threshold |
| R3 | FSM stuck in EMERGENCY | `grinder_fsm_current_state{state="EMERGENCY"} == 1` for >10 min | 10 consecutive minutes |
| R4 | Emergency exit error | `grinder_emergency_exit_total{result="error"}` incrementing | Any error result |
| R5 | Account sync stalled | `grinder_account_sync_age_seconds > 120` | 2 minutes without sync |
| R6 | HA flapping | `grinder_ha_role` changes >3 times in 5 min | 3 role changes / 5 min |
| R7 | Fill-prob CB trip | `grinder_router_fill_prob_cb_trips_total > 0` | Any trip (see Runbook 32) |
| R8 | Operator doubt | Anything unexpected | Judgment call |

**Metrics that do NOT exist** and therefore cannot be used as rollback criteria:
account sync has no `consecutive_failure` counter; HA has no `flap_count` metric.
R5 and R6 require manual observation of the gauges listed above.

---

## 5. Rollback Procedure

### 5.1 Immediate stop (fastest, <1 min)

```bash
# Option A: Kill-switch via operator override (no restart needed)
export GRINDER_OPERATOR_OVERRIDE=EMERGENCY
# FSM immediately enters EMERGENCY → blocks INCREASE_RISK intents.
# Emergency exit (if enabled) will cancel orders + close positions.

# Option B: Full stop
kill -SIGINT <pid>
# or:
docker compose stop grinder
```

### 5.2 Controlled rollback (env flip + restart)

```bash
# 1. Disable live trading + FSM
export GRINDER_TRADING_MODE=read_only
export GRINDER_FSM_ENABLED=0
unset GRINDER_TRADING_LOOP_ACK
unset ALLOW_MAINNET_TRADE

# 2. Restart
docker compose restart grinder
# or kill + relaunch with new env

# 3. Verify rollback took effect
curl -sf http://localhost:9090/healthz | python3 -m json.tool
# Expected: {"status": "ok", ...}

curl -sf http://localhost:9090/metrics | grep -E "kill_switch_triggered|readyz_ready|fsm_current_state"
# Expected: kill_switch_triggered 0, readyz_ready 1 (or 0 if HA standby),
#           fsm_current_state{state="INIT"} 1 (fresh start)
```

### 5.3 Verify no new orders after rollback

```bash
# Check order attempt metrics are stable (no new increments)
BEFORE=$(curl -sf http://localhost:9090/metrics | grep 'grinder_port_order_attempts_total' | awk '{sum+=$2} END{print sum+0}')
sleep 60
AFTER=$(curl -sf http://localhost:9090/metrics | grep 'grinder_port_order_attempts_total' | awk '{sum+=$2} END{print sum+0}')
[ "$BEFORE" = "$AFTER" ] && echo "ROLLBACK OK: no new orders" || echo "WARNING: orders still attempted"
```

---

## 6. Evidence Artifacts

Save after every activation ceremony (success or rollback):

```bash
CEREMONY_DIR="artifacts/ceremony_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$CEREMONY_DIR"

# 1. Commit hash
git rev-parse HEAD > "$CEREMONY_DIR/commit.txt"

# 2. Env snapshot (exclude secrets)
env | grep -E "GRINDER_|ALLOW_|BINANCE_API_KEY" | sed 's/BINANCE_API_SECRET=.*/BINANCE_API_SECRET=<redacted>/' | sort > "$CEREMONY_DIR/env.txt"

# 3. Metrics snapshot
curl -sf http://localhost:9090/metrics > "$CEREMONY_DIR/metrics.prom"

# 4. Key metrics summary
curl -sf http://localhost:9090/metrics | grep -E \
  "kill_switch_triggered|drawdown_pct|fsm_current_state|emergency_exit|account_sync_age|readyz_ready|engine_initialized" \
  > "$CEREMONY_DIR/metrics_summary.txt"

# 5. FSM evidence artifacts (if GRINDER_FSM_EVIDENCE=1)
cp -r "${GRINDER_ARTIFACT_DIR:-artifacts}/fsm/" "$CEREMONY_DIR/" 2>/dev/null || true
```

---

## Related Runbooks

| Runbook | Relevance |
|---------|-----------|
| [04 Kill-Switch](04_KILL_SWITCH.md) | Kill-switch trip, drill, and recovery (restart only) |
| [22 ACTIVE Enablement](22_ACTIVE_ENABLEMENT_CEREMONY.md) | Reconcile loop activation (separate from trading loop) |
| [27 FSM Operator Override](27_FSM_OPERATOR_OVERRIDE.md) | Force FSM to PAUSE/EMERGENCY via env var |
| [32 Mainnet Rollout](32_MAINNET_ROLLOUT_FILL_PROB.md) | Fill-prob enforcement ceremony (Phase 0-5) |
