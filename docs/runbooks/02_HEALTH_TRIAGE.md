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

---

## Common Issues

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| Connection refused | Container not running | `docker compose up -d` |
| Timeout | Container overloaded | Check logs, consider restart |
| `uptime_s` very low | Recent crash/restart | Check logs for crash reason |
| `grinder_up 0` | Graceful shutdown in progress | Wait or investigate |
| `kill_switch_triggered 1` | Risk limit breached | See kill-switch runbook |
