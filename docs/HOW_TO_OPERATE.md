# How to Operate GRINDER

Operator's guide for running and maintaining the GRINDER trading system.

---

## What Is GRINDER

GRINDER is an automated trading system that:
- Connects to exchange data feeds
- Generates trading signals based on ML models
- Executes trades with risk controls
- Exposes metrics for monitoring

**Current mode:** Dry-run (paper trading) by default.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
├─────────────────┬─────────────────┬─────────────────────┤
│    grinder      │   prometheus    │      grafana        │
│    :9090        │     :9091       │       :3000         │
│                 │                 │                     │
│  /healthz       │   Scrapes       │   Dashboards        │
│  /metrics       │   metrics       │   Alerts            │
└─────────────────┴─────────────────┴─────────────────────┘
```

### Components

| Service | Port | Purpose |
|---------|------|---------|
| grinder | 9090 | Main application (metrics, health) |
| prometheus | 9091 | Metrics storage and alerting |
| grafana | 3000 | Visualization and dashboards |

---

## Quick Start

### 1. Clone and Configure

```bash
git clone <repo-url>
cd grinder
cp .env.example .env
# Edit .env if needed
```

### 2. Start Stack

```bash
docker compose -f docker-compose.observability.yml up --build -d
```

### 3. Verify Health

```bash
curl -fsS http://localhost:9090/healthz
# Expected: {"status": "ok", "uptime_s": ...}
```

### 4. Open Dashboards

- Grafana: http://localhost:3000 (admin/admin)
- Prometheus: http://localhost:9091

---

## Daily Operations

### Morning Checklist

1. **Verify system is running:**
   ```bash
   curl -fsS http://localhost:9090/healthz
   ```

2. **Check kill-switch status:**
   ```bash
   curl -fsS http://localhost:9090/metrics | grep kill_switch_triggered
   # Should be 0
   ```

3. **Review overnight alerts** in Grafana

### Monitoring During Trading Hours

- Keep Grafana dashboard open
- Watch for:
  - Kill Switch panel turning red
  - Drawdown % rising above 3%
  - Blocked Total increasing rapidly

### End of Day

- Review metrics summary
- Check for any triggered alerts
- No action needed if all normal

---

## Deploying a Release

### Current Process (Simple)

1. **Merge PR to main**
   - All CI checks must pass
   - Soak gate must be green

2. **Pull latest on server**
   ```bash
   git pull origin main
   ```

3. **Restart with new code**
   ```bash
   docker compose -f docker-compose.observability.yml down
   docker compose -f docker-compose.observability.yml up --build -d
   ```

4. **Verify deployment**
   ```bash
   # Check health
   curl -fsS http://localhost:9090/healthz

   # Check metrics are flowing
   curl -fsS http://localhost:9090/metrics | head

   # Check logs for errors
   docker logs grinder --tail=50
   ```

### What "Good" Looks Like After Deploy

- `/healthz` returns `{"status": "ok"}`
- All metrics present in `/metrics`
- No errors in logs
- Grafana dashboard showing data
- No alerts firing

---

## Key Endpoints

| Endpoint | Method | Purpose | Expected Response |
|----------|--------|---------|-------------------|
| `/healthz` | GET | Liveness check | `{"status": "ok", "uptime_s": N}` |
| `/metrics` | GET | Prometheus metrics | Prometheus text format |

---

## Important Metrics

| Metric | Normal Value | Action If Abnormal |
|--------|--------------|-------------------|
| `grinder_up` | 1 | Check container status |
| `grinder_kill_switch_triggered` | 0 | See kill-switch runbook |
| `grinder_drawdown_pct` | < 3 | Monitor closely if rising |

---

## Runbooks

For detailed procedures, see [docs/runbooks/](runbooks/):

| Runbook | When to Use |
|---------|-------------|
| [Startup/Shutdown](runbooks/01_STARTUP_SHUTDOWN.md) | Starting or stopping the system |
| [Health Triage](runbooks/02_HEALTH_TRIAGE.md) | Quick diagnostics |
| [Metrics & Dashboards](runbooks/03_METRICS_DASHBOARDS.md) | Understanding metrics |
| [Kill-Switch](runbooks/04_KILL_SWITCH.md) | When trading is halted |
| [Soak Gate](runbooks/05_SOAK_GATE.md) | Validating releases |
| [Alert Response](runbooks/06_ALERT_RESPONSE.md) | Responding to alerts |

---

## Troubleshooting Quick Reference

| Problem | Quick Check | Solution |
|---------|-------------|----------|
| Can't reach /healthz | `docker ps` | Restart container |
| Kill-switch triggered | Check `drawdown_pct` | Wait for reset or restart |
| High block rate | Check `/metrics` for reasons | Review gating configuration |
| Container restarting | `docker logs grinder` | Fix error, rebuild |
| Metrics missing in Grafana | Check Prometheus targets | Verify network |

---

## Contacts

For issues not covered in runbooks:
- Check logs: `docker logs grinder --tail=200`
- Review recent commits for changes
- Escalate if data loss or financial impact possible
