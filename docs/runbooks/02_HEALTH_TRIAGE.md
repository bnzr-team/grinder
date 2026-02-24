# Runbook: Health Triage

## Overview

Quick diagnostics to determine if GRINDER is alive and functioning correctly.

---

## Quick Health Check

### 1. Check `/healthz` Endpoint

```bash
curl -fsS http://localhost:9090/healthz
```

**What good looks like:**

```json
{"status": "ok", "uptime_s": 3600.25}
```

**What bad looks like:**

- Connection refused: Container not running or port not exposed
- Timeout: Container overloaded or deadlocked
- 5xx error: Internal application error

### 2. Check Container Status

```bash
docker ps -f name=grinder --format "table {{.Names}}\t{{.Status}}"
```

**What good looks like:**

```
NAMES     STATUS
grinder   Up 2 hours (healthy)
```

**What bad looks like:**

```
NAMES     STATUS
grinder   Up 5 minutes (unhealthy)
grinder   Restarting (1) 10 seconds ago
```

---

## Triage Decision Tree

```
Is /healthz reachable?
├── NO → Is container running? (docker ps)
│   ├── NO → Start container (see 01_STARTUP_SHUTDOWN.md)
│   └── YES → Check logs: docker logs grinder --tail=50
└── YES → Is status "ok"?
    ├── NO → Check /metrics for error counters
    └── YES → System healthy, check metrics for warnings
```

---

## Detailed Health Checks

### Check Uptime

```bash
curl -fsS http://localhost:9090/healthz | jq '.uptime_s'
```

**Interpretation:**
- `uptime_s < 60`: Recent restart, check if intentional
- `uptime_s > 86400`: Running for >1 day, good stability

### Check Metrics Endpoint

```bash
curl -fsS http://localhost:9090/metrics | grep -E "^grinder_up|^grinder_uptime"
```

**What good looks like:**

```
grinder_up 1
grinder_uptime_seconds 3600.25
```

**What bad looks like:**

```
grinder_up 0
```

### Check Kill-Switch Status

```bash
curl -fsS http://localhost:9090/metrics | grep "grinder_kill_switch"
```

**What good looks like:**

```
grinder_kill_switch_triggered 0
```

**What bad looks like (trading halted):**

```
grinder_kill_switch_triggered 1
```

If kill-switch is triggered, see [04_KILL_SWITCH.md](04_KILL_SWITCH.md).

---

## Launch-13/14/15 Alert Response

### FSM Alerts (Launch-13)

| Alert | Severity | Action |
|-------|----------|--------|
| `FsmBadStateTooLong` | warning | FSM stuck in DEGRADED/EMERGENCY/PAUSED >2min. Check underlying trigger: kill-switch (`grinder_kill_switch_triggered`), feed stale, drawdown. See [27_FSM_OPERATOR_OVERRIDE.md](27_FSM_OPERATOR_OVERRIDE.md). |
| `FsmActionBlockedSpike` | warning | Intents blocked by FSM permission matrix. Check current state (`grinder_fsm_current_state`), consider operator override if state is incorrect. See [27_FSM_OPERATOR_OVERRIDE.md](27_FSM_OPERATOR_OVERRIDE.md). |

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep grinder_fsm_current_state
curl -fsS http://localhost:9090/metrics | grep grinder_fsm_action_blocked_total
```

### SOR Alerts (Launch-14)

| Alert | Severity | Action |
|-------|----------|--------|
| `SorBlockedSpike` | warning | Orders rejected by router. Check block reasons: `grinder_router_decision_total{decision="BLOCK"}`. Run fire drill: `bash scripts/ops_fill_triage.sh sor-fire-drill`. See [28_SOR_FIRE_DRILL.md](28_SOR_FIRE_DRILL.md). |
| `SorNoopSpike` | info | Orders skipped (already in desired state). Normal during low-activity periods. Investigate if sustained. See [28_SOR_FIRE_DRILL.md](28_SOR_FIRE_DRILL.md). |

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep grinder_router_decision_total
```

### AccountSync Alerts (Launch-15)

| Alert | Severity | Action |
|-------|----------|--------|
| `AccountSyncStale` | warning | Sync data >120s old. Check API connectivity, credentials, and `GRINDER_ACCOUNT_SYNC_ENABLED=1`. See [29_ACCOUNT_SYNC.md](29_ACCOUNT_SYNC.md). |
| `AccountSyncErrors` | warning | HTTP/auth/parse failures during sync. Check Binance API status, API key validity. See [29_ACCOUNT_SYNC.md](29_ACCOUNT_SYNC.md). |
| `AccountSyncMismatchSpike` | warning | Expected vs observed state diverged. Run fire drill: `bash scripts/ops_fill_triage.sh account-sync-drill`. See [30_ACCOUNT_SYNC_FIRE_DRILL.md](30_ACCOUNT_SYNC_FIRE_DRILL.md). |

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep grinder_account_sync
```

### Consecutive Loss Guard (PR-C3b/C3c)

| Alert | Severity | Action |
|-------|----------|--------|
| `ConsecutiveLossTrip` | critical | Guard tripped — PAUSE set via `GRINDER_OPERATOR_OVERRIDE`. Check if losses are real or a data issue. Clear override to resume: `unset GRINDER_OPERATOR_OVERRIDE` + restart. |
| `ConsecutiveLossesHigh` | warning | Early warning: >=3 consecutive losses (guard trips at configured threshold, default 5). Monitor closely. |

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep grinder_risk_consecutive
```

**Env vars:**

| Variable | Default | Description |
|----------|---------|-------------|
| `GRINDER_CONSEC_LOSS_ENABLED` | `false` | Enable guard |
| `GRINDER_CONSEC_LOSS_THRESHOLD` | `5` | Consecutive losses before trip |
| `GRINDER_CONSEC_LOSS_EVIDENCE` | `false` | Write evidence artifacts on trip |
| `GRINDER_CONSEC_LOSS_STATE_PATH` | none | Path to persist guard state (JSON + sha256 sidecar) |

**State file (PR-C3d):**
- Format: `consecutive_loss_state_v2` JSON + `.sha256` sidecar (v1 files auto-upgraded on load).
- Contains per-symbol guard states, `last_trade_id` cursor, cumulative `trip_count`, and RoundtripTracker open positions.
- Inspect: `cat $GRINDER_CONSEC_LOSS_STATE_PATH | python3 -m json.tool`
- Reset guard: delete state file and restart (`rm $GRINDER_CONSEC_LOSS_STATE_PATH*`).
- Per-symbol attribution: check evidence artifacts in `$GRINDER_ARTIFACT_DIR/risk/` for symbol-level detail.
- Tracker recovery: after restart, in-flight roundtrips (entry before restart) are restored from state file. Exit fills after restart close them normally. Look for `CONSEC_LOSS_TRACKER_RESTORED` in logs.
- Tracker restore failure: if state file tracker data is corrupt, service logs `CONSEC_LOSS_TRACKER_RESTORE_FAILED` and starts with fresh tracker. Guards and dedup cursor are still restored.

**Recovery:**
1. Investigate: are losses real? Check `grinder_risk_consecutive_losses` count (= max across all symbols).
2. If false positive: clear override, restart, adjust threshold.
3. If real: keep PAUSE, investigate strategy.
4. To fully reset: delete state file (`rm $GRINDER_CONSEC_LOSS_STATE_PATH*`) + restart.

**Scope:** Wired only in `scripts/run_live_reconcile.py`. Other entrypoints do not activate this guard.

### Engine Initialization Failures

**Alert:** `EngineInitDown` (critical) — `grinder_live_engine_initialized == 0` for 5+ minutes while process is up.

**What it means:** The trading engine did not complete initialization. The process is running but no ticks are being processed.

| Check | Command |
|-------|---------|
| Engine gauge | `curl -fsS http://localhost:9090/metrics \| grep grinder_live_engine_initialized` |
| Process up | `curl -fsS http://localhost:9090/metrics \| grep grinder_up` |
| Startup logs | `docker logs grinder --tail=100 \| grep -i "engine\|init\|error\|fatal"` |

**What good looks like** (`curl -fsS http://localhost:9090/metrics | grep -E 'grinder_live_engine_initialized|grinder_up '`):

```
grinder_live_engine_initialized 1
grinder_up 1
```

**What bad looks like:**

```
grinder_live_engine_initialized 0
grinder_up 1
```

**PromQL** (adjust job/instance selectors for your environment):

```promql
grinder_live_engine_initialized == 0 and grinder_up == 1
```

**Common causes:**
- Fill model load failure (`GRINDER_FILL_MODEL_DIR` misconfigured) — check for `Fill model load FAILED` in logs
- Missing or invalid config (symbols, mode, exchange port)
- Import/dependency error on startup

**Recovery:**
1. Check logs for specific error: `docker logs grinder --tail=200`
2. Verify env vars: `GRINDER_TRADING_MODE`, symbols, `GRINDER_FILL_MODEL_DIR`
3. If config issue: fix env and restart
4. If dependency issue: check image version, rebuild if needed

### Readyz Not Ready

**Alert:** `ReadyzNotReady` (warning) — readyz returning not-ready for 5+ minutes while process is up and callback is registered.

**What it means:** The trading loop started but `/readyz` returns 503. This is normal during HA standby; investigate if unexpected.

| Check | Command |
|-------|---------|
| Readyz endpoint | `curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/readyz` |
| Readyz body | `curl -fsS http://localhost:9090/readyz \| python3 -m json.tool` |
| Readyz gauges | `curl -fsS http://localhost:9090/metrics \| grep grinder_readyz` |
| HA role | `curl -fsS http://localhost:9090/metrics \| grep grinder_ha_role` |

**What good looks like** (`curl -fsS http://localhost:9090/metrics | grep -E 'grinder_readyz_(callback_registered|ready) '`):

```
grinder_readyz_callback_registered 1
grinder_readyz_ready 1
```

**What bad looks like:**

```
grinder_readyz_callback_registered 1
grinder_readyz_ready 0
```

**PromQL** (adjust job/instance selectors for your environment):

```promql
grinder_readyz_callback_registered == 1 and grinder_readyz_ready == 0 and grinder_up == 1
```

**Interpreting readyz body:**

| `ha_enabled` | `ha_role` | `loop_ready` | `ready` | Meaning |
|:---:|:---:|:---:|:---:|---|
| false | n/a | true | true | Healthy (no HA) |
| true | active | true | true | Healthy (HA active) |
| true | standby | true | false | Normal standby — not an issue |
| true | unknown | true | false | HA elector failed — check Redis connectivity |
| any | any | false | false | Loop not ready — check engine init and logs |

**Recovery:**
1. If `ha_role=standby`: normal, no action needed (alert should not fire in steady state — elector promotes within seconds)
2. If `ha_role=unknown`: check Redis connectivity (`GRINDER_REDIS_URL`), restart elector
3. If `loop_ready=false`: check `grinder_live_engine_initialized` gauge — if 0, see [Engine Initialization Failures](#engine-initialization-failures)
4. If all looks fine but still not ready: check logs for async errors, restart process

### Observability Quick Check

Single command to grep all Launch-13/14/15 metrics at once (no Prometheus needed):

```bash
curl -fsS http://localhost:9090/metrics | grep -E \
  "grinder_fsm_current_state|grinder_fsm_state_duration|grinder_fsm_action_blocked|grinder_router_decision_total|grinder_account_sync_age|grinder_account_sync_errors|grinder_account_sync_mismatches"
```

If you have Prometheus running, paste these PromQL queries for a full picture:

- FSM state: `grinder_fsm_current_state`
- SOR decisions: `sum by (decision,reason) (increase(grinder_router_decision_total[5m]))`
- Sync freshness: `grinder_account_sync_age_seconds`

For detailed panel definitions and drilldowns, see [OBSERVABILITY_STACK.md -- Launch-13/14/15 Quick Panels](../OBSERVABILITY_STACK.md#launch-131415-quick-panels).

### Unexpected Futures HTTP

**Alert:** `FuturesHttpRequestsDetected` (critical) — HTTP requests detected from futures port during rehearsal/fixture run.

**What it means:** The `grinder_port_http_requests_total{port="futures"}` counter incremented, indicating real network calls to Binance Futures API endpoints. In a fixture/rehearsal context, this should never happen.

| Check | Command |
|-------|---------|
| HTTP requests by route | `curl -fsS http://localhost:9090/metrics \| grep 'grinder_port_http_requests_total{port="futures"'` |
| PromQL (5m window) | `sum(increase(grinder_port_http_requests_total{port="futures"}[5m]))` |
| Order attempts | `curl -fsS http://localhost:9090/metrics \| grep 'grinder_port_order_attempts_total{port="futures"'` |

**Common causes:**
- Process running with `--exchange-port futures` against real ticks (not fixture)
- Incorrect env vars: missing `GRINDER_TRADING_MODE`, `ALLOW_MAINNET_TRADE`, `GRINDER_REAL_PORT_ACK`, or wrong API keys
- Someone started a rehearsal process without `--fixture` flag

**Recovery:**
1. Stop the process immediately
2. Verify env vars: `GRINDER_TRADING_MODE`, `ALLOW_MAINNET_TRADE`, `GRINDER_REAL_PORT_ACK`, API keys
3. For rehearsal: use `--exchange-port noop` + `--fixture <path>` to prevent any real API traffic
4. Review logs for which routes were called: `grep '/fapi/' <logfile>`

**Dashboard drilldown:**

1. Open **Grinder Trading Loop** dashboard (uid: `grinder-trading-loop`)
2. Check **Futures HTTP by Route+Method (top 10, 5m)** — shows which endpoints were hit and with which HTTP method
3. Check **Futures HTTP by Route+Method (1h)** — shows if the pattern is sustained or a one-time blip
4. Use the **All Ports HTTP Requests (5m by port)** panel to compare futures vs other ports

**Edge cases:**

| Pattern | Meaning | Action |
|---------|---------|--------|
| Only `GET /fapi/v1/exchangeInfo` | SDK/client initialization call | Low risk — verify no order routes present |
| `POST /fapi/v1/order` or `DELETE /fapi/v1/order` | Real order submission/cancellation | **Stop immediately** — this is live trading |
| `GET /fapi/v1/openOrders` or `GET /fapi/v2/account` | Account state queries | Medium risk — process is reading real state |
| `GET /fapi/v1/ping` or `GET /fapi/v1/time` | Health/time check | Low risk — likely SDK keepalive |

**Confirming it's a real Binance endpoint:**

- Routes starting with `/fapi/` are Binance Futures API
- Routes starting with `/api/` are Binance Spot API
- PromQL drilldown: `sum by (route, method) (increase(grinder_port_http_requests_total{port="futures"}[5m]))`

### Fill Probability Gate Blocking (FillProbBlocksSpike / FillProbBlocksHigh)

**Alert:** `FillProbBlocksSpike` (warning) — fill probability gate blocking orders for 5+ minutes.
**Alert:** `FillProbBlocksHigh` (critical) — >=10 fill-prob blocks in 5 minutes.

**What it means:** The fill probability model is rejecting orders below the configured threshold. A few blocks are normal; sustained heavy blocking means the model or threshold needs recalibration.

| Check | Command |
|-------|---------|
| Block count | `curl -fsS http://localhost:9090/metrics \| grep grinder_router_fill_prob_blocks_total` |
| CB trips | `curl -fsS http://localhost:9090/metrics \| grep grinder_router_fill_prob_cb_trips_total` |
| Enforcement | `curl -fsS http://localhost:9090/metrics \| grep grinder_router_fill_prob_enforce_enabled` |
| Auto-threshold | `curl -fsS http://localhost:9090/metrics \| grep grinder_router_fill_prob_auto_threshold_bps` |

**What good looks like** (`curl -fsS http://localhost:9090/metrics | grep -E 'router_fill_prob_(blocks|cb_trips|enforce|auto_threshold)'`):

```
grinder_router_fill_prob_blocks_total 3
grinder_router_fill_prob_cb_trips_total 0
grinder_router_fill_prob_enforce_enabled 1
grinder_router_fill_prob_auto_threshold_bps 4200
```

**What bad looks like:**

```
grinder_router_fill_prob_blocks_total 47
grinder_router_fill_prob_cb_trips_total 2
grinder_router_fill_prob_enforce_enabled 1
```

**PromQL** (adjust job/instance selectors for your environment):

```promql
increase(grinder_router_fill_prob_blocks_total[5m])
increase(grinder_router_fill_prob_cb_trips_total[5m])
```

**Decision tree:**

```
Is FillProbBlocksHigh firing (>=10 blocks/5m)?
├── YES → Check CB trips (grinder_router_fill_prob_cb_trips_total)
│   ├── CB tripped → Gate auto-bypassed. Rollback: GRINDER_FILL_MODEL_ENFORCE=0 + restart.
│   └── CB not tripped → Threshold too aggressive. Review eval report, consider raising threshold.
└── NO (FillProbBlocksSpike only, <10 blocks/5m)
    └── Normal operation — some blocks expected. Monitor.
```

**Recovery:**
1. **Immediate rollback:** `export GRINDER_FILL_MODEL_ENFORCE=0` + restart (disables fill-prob gate entirely)
2. **Investigate:** `curl -fsS http://localhost:9090/metrics | grep -E 'router_fill_prob_(auto_threshold|blocks|cb_trips|enforce)'`
3. **Full procedure:** See [31_FILL_PROB_ROLLOUT.md](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) for rollout/rollback/calibration steps

---

## Common Issues

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| Connection refused | Container not running | `docker compose up -d` |
| Timeout | Container overloaded | Check logs, consider restart |
| `uptime_s` very low | Recent crash/restart | Check logs for crash reason |
| `grinder_up 0` | Graceful shutdown in progress | Wait or investigate |
| `kill_switch_triggered 1` | Risk limit breached | See kill-switch runbook |
