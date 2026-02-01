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

## Common Issues

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| Connection refused | Container not running | `docker compose up -d` |
| Timeout | Container overloaded | Check logs, consider restart |
| `uptime_s` very low | Recent crash/restart | Check logs for crash reason |
| `grinder_up 0` | Graceful shutdown in progress | Wait or investigate |
| `kill_switch_triggered 1` | Risk limit breached | See kill-switch runbook |
