# Runbook: Alert Response

## Overview

Response procedures for Prometheus alerts defined in `monitoring/alert_rules.yml`.

---

## Alert Summary

| Alert | Severity | Condition | Response Time |
|-------|----------|-----------|---------------|
| GrinderDown | critical | `grinder_up == 0` for 1m | Immediate |
| GrinderTargetDown | critical | Target unreachable for 1m | Immediate |
| KillSwitchTripped | critical | `kill_switch_triggered == 1` | Immediate |
| HighDrawdown | warning | `drawdown_pct > 3` for 1m | Within 15m |
| HighGatingBlocks | warning | Block rate > 0.1/s for 5m | Within 1h |
| KillSwitchTripIncreased | info | Trip count increased in 5m | Next business day |
| GrinderRecentRestart | info | `uptime < 60s` | Acknowledge |

---

## Critical Alerts

### GrinderDown

**Condition:** `grinder_up == 0` for 1 minute

**Impact:** System is not running, no trading possible.

**Response:**

1. Check container status:
   ```bash
   docker ps -a -f name=grinder
   ```

2. Check logs for crash reason:
   ```bash
   docker logs grinder --tail=100
   ```

3. Restart if needed:
   ```bash
   docker compose -f docker-compose.observability.yml restart grinder
   ```

4. Verify recovery:
   ```bash
   curl -fsS http://localhost:9090/healthz
   ```

---

### GrinderTargetDown

**Condition:** Prometheus cannot scrape grinder for 1 minute

**Impact:** Metrics not being collected, alerts won't fire.

**Response:**

1. Check if grinder is running:
   ```bash
   curl -fsS http://localhost:9090/metrics | head
   ```

2. Check Prometheus targets:
   Open http://localhost:9091/targets

3. Check network between containers:
   ```bash
   docker network inspect grinder_default
   ```

---

### KillSwitchTripped

**Condition:** `grinder_kill_switch_triggered == 1`

**Impact:** All trading is halted.

**Response:**

See [04_KILL_SWITCH.md](04_KILL_SWITCH.md) for full procedure.

Quick summary:
1. Verify state: `curl -fsS localhost:9090/metrics | grep kill_switch`
2. Check reason: Look at `trips_total` labels
3. Decide: Wait for auto-reset or manual restart

---

## Warning Alerts

### HighDrawdown

**Condition:** `grinder_drawdown_pct > 3` for 1 minute

**Impact:** Approaching kill-switch threshold (5%).

**Response:**

1. Monitor drawdown:
   ```bash
   watch -n5 'curl -s localhost:9090/metrics | grep drawdown_pct'
   ```

2. Review recent trades in logs

3. Consider manual intervention if drawdown continues rising

4. If drawdown reaches 5%, kill-switch will trigger automatically

---

### HighGatingBlocks

**Condition:** Block rate > 0.1 per second for 5 minutes

**Impact:** Many trade signals being blocked.

**Response:**

1. Check block reasons:
   ```bash
   curl -fsS localhost:9090/metrics | grep "gating_blocked_total"
   ```

2. Common reasons:
   - `toxicity`: Model flagging high-risk trades
   - `position_limit`: Max position reached
   - `cooldown`: Rate limiting active

3. Review if blocks are expected given market conditions

---

## Info Alerts

### KillSwitchTripIncreased

**Condition:** Kill-switch trip count increased in last 5 minutes

**Impact:** A kill-switch event occurred (may have already recovered).

**Response:**

1. Check current state:
   ```bash
   curl -fsS localhost:9090/metrics | grep kill_switch
   ```

2. Review what triggered the trip

3. Document for post-mortem

---

### GrinderRecentRestart

**Condition:** Uptime < 60 seconds

**Impact:** System recently restarted.

**Response:**

1. Verify restart was intentional

2. If unintentional, check logs for crash:
   ```bash
   docker logs grinder --tail=200
   ```

3. Monitor for stability (no repeated restarts)

---

## Alert Silencing

### Temporary Silence (Maintenance)

During planned maintenance, silence alerts in Prometheus Alertmanager:

```bash
# Example: Silence all grinder alerts for 1 hour
curl -X POST http://localhost:9093/api/v2/silences -d '{
  "matchers": [{"name": "job", "value": "grinder"}],
  "startsAt": "2024-01-01T00:00:00Z",
  "endsAt": "2024-01-01T01:00:00Z",
  "createdBy": "operator",
  "comment": "Planned maintenance"
}'
```

**Note:** Alertmanager is not currently deployed in the observability stack. Silencing would need to be done via Grafana or by modifying alert rules.

---

## Escalation

| Severity | Response Time | Escalation |
|----------|---------------|------------|
| Critical | Immediate | Page on-call |
| Warning | 15 minutes | Slack notification |
| Info | Next business day | Email summary |
