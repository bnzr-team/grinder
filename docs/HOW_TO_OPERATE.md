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

### Release Checklist v1

This is the canonical checklist for deploying a release to production.

#### Pre-flight

- [ ] **PR merged to main** with all CI checks green:
  - [ ] `checks` (ruff, mypy, pytest)
  - [ ] `soak-gate` PASS
  - [ ] `docker-smoke` PASS (includes `/readyz` verification)
  - [ ] `ha-smoke` PASS (if HA deployment)
  - [ ] `determinism-suite` PASS

  **Note:** Some workflows are path-filtered and may not auto-trigger on docs-only PRs.
  To verify CI status: `gh pr checks <PR_NUMBER>`
  To manually re-run a workflow: `gh run rerun <RUN_ID>` or use GitHub Actions UI.

- [ ] **No pending migrations** (check DECISIONS.md for breaking changes)
- [ ] **Config verified**: environment variables match deployment target
- [ ] **Rollback plan ready** (see below)

#### Deployment Steps

1. **Pull latest on server:**
   ```bash
   git pull origin main
   ```

2. **For single-instance deployment:**
   ```bash
   docker compose -f docker-compose.observability.yml down
   docker compose -f docker-compose.observability.yml up --build -d
   ```

3. **For HA deployment (rolling restart):**
   See [07_HA_OPERATIONS.md](runbooks/07_HA_OPERATIONS.md#rolling-restart-zero-downtime-http) for zero-downtime procedure.

4. **Verify deployment:**
   ```bash
   # Health check
   curl -fsS http://localhost:9090/healthz
   # Expected: {"status": "ok", "uptime_s": ...}

   # Readiness check (for HA)
   curl -fsS http://localhost:9090/readyz
   # Expected: {"ready": true, "role": "active"} or role=unknown in non-HA

   # Metrics flowing
   curl -fsS http://localhost:9090/metrics | head

   # No errors in logs
   docker logs grinder --tail=50 | grep -i error
   ```

#### Post-deployment Verification

- [ ] `/healthz` returns `{"status": "ok"}`
- [ ] `/readyz` returns expected role (active/standby/unknown)
- [ ] All metrics present in `/metrics`
- [ ] Grafana dashboard showing data
- [ ] **Alerts quiet** for 5 minutes after deploy
- [ ] No errors in container logs

#### Rollback Plan

If deployment fails or causes issues:

1. **Identify the problem:**
   ```bash
   docker logs grinder --tail=100
   curl -fsS http://localhost:9090/healthz
   ```

2. **Quick rollback (revert to previous image):**
   ```bash
   git checkout HEAD~1
   docker compose -f docker-compose.observability.yml up --build -d
   ```

3. **Verify rollback:**
   ```bash
   curl -fsS http://localhost:9090/healthz
   ```

4. **Escalate:** If rollback doesn't fix the issue, check recent commits and DECISIONS.md for breaking changes.

#### Signals to Watch After Deploy

| Metric | Normal | Action if Abnormal |
|--------|--------|-------------------|
| `grinder_up` | 1 | Container not running |
| `grinder_kill_switch_triggered` | 0 | Check kill-switch runbook |
| `grinder_ha_role{role="active"}` | 1 on one instance | Check HA runbook |
| Container restarts | 0 | Check logs for crash loop |

---

## Key Endpoints

| Endpoint | Method | Purpose | Expected Response |
|----------|--------|---------|-------------------|
| `/healthz` | GET | Liveness check | `{"status": "ok", "uptime_s": N}` |
| `/readyz` | GET | Readiness check (HA-aware) | `{"ready": true/false, "role": "active/standby/unknown"}` |
| `/metrics` | GET | Prometheus metrics | Prometheus text format |

**Note:** `/readyz` returns HTTP 200 if ACTIVE, HTTP 503 if STANDBY or UNKNOWN. In non-HA mode, role is "unknown" and returns 503.

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
| [HA Operations](runbooks/07_HA_OPERATIONS.md) | High availability deployment |

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
