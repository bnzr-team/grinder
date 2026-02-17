# Runbook 21: Single-Venue Launch Readiness (Launch-01)

Step-by-step rollout procedure for GRINDER on a single venue (Binance USDT-M).

## Overview

Launch follows a staged rollout: **SHADOW → STAGING → ACTIVE**. Each stage has explicit preconditions, verification criteria, and minimum soak duration before proceeding to the next.

**Stages:**

| Stage | Mode | Orders | Duration |
|-------|------|--------|----------|
| SHADOW | Dry-run, detect-only | None (paper) | >= 24h |
| STAGING | Plan-only, dry-run remediation | None (paper) | >= 12h |
| ACTIVE | Armed, real orders, conservative limits | Real (limited) | Ongoing |

---

## Before-Enable Checklist

Complete **all** items before starting Stage 1:

- [ ] **CI green**: All GitHub Actions checks pass on `main`
- [ ] **Launch readiness smoke**: `bash scripts/smoke_launch_readiness.sh` → EXIT 0
- [ ] **Kill-switch smoke**: Kill-switch metric at zero in /metrics
- [ ] **Docker image builds**: `docker build -t grinder:launch .` succeeds
- [ ] **`.env` configured**: Copy `.env.example` → `.env`, fill API credentials
- [ ] **Symbol whitelist**: Decided and documented (start with 1-2 symbols)
- [ ] **Notional caps**: Conservative limits set (see Stage 3)
- [ ] **Team notified**: Announce go-live window
- [ ] **Monitoring ready**: Prometheus + Grafana accessible (see [Runbook 03](03_METRICS_DASHBOARDS.md))

---

## Stage 1: SHADOW (Dry-Run, Detect-Only)

**Goal:** Verify data flows, feature computation, and metric emission without any order activity.

### 1.1 Configuration

```bash
export GRINDER_MODE=dry-run
export RECONCILE_ENABLED=1
export RECONCILE_ACTION=none        # No remediation
export RECONCILE_DRY_RUN=true       # Extra safety
export ARMED=0
unset ALLOW_MAINNET_TRADE
```

### 1.2 Start

Follow [Runbook 01](01_STARTUP_SHUTDOWN.md) startup procedure:

```bash
docker compose up -d grinder
```

### 1.3 Verify

```bash
# Health
curl -sf http://localhost:9090/healthz | python3 -m json.tool
# Expected: {"status": "ok", "uptime_s": ...}

# Metrics emitting
curl -sf http://localhost:9090/metrics | grep grinder_up
# Expected: grinder_up 1

# Reconcile loop running (if enabled)
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total
# Expected: increasing over time

# Kill switch NOT triggered
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: grinder_kill_switch_triggered 0

# No executions
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_action_executed_total
# Expected: 0 or absent
```

### 1.4 Pass Criteria

- [ ] `/healthz` returns 200 with `status` + `uptime_s`
- [ ] `/metrics` passes SSOT contract (`python3 -m scripts.smoke_metrics_contract --url http://localhost:9090/metrics`)
- [ ] `reconcile_runs_total` increasing
- [ ] `action_executed_total` = 0
- [ ] `kill_switch_triggered` = 0
- [ ] Soak for **minimum 24 hours**

---

## Stage 2: STAGING (Plan-Only, Dry-Run Remediation)

**Goal:** Enable remediation planning to verify plans look correct, without executing any orders.

### 2.1 Configuration

```bash
export GRINDER_MODE=dry-run
export RECONCILE_ENABLED=1
export RECONCILE_ACTION=cancel_all   # Or: flatten, auto
export RECONCILE_DRY_RUN=true        # Plans only, no execution
export ALLOW_ACTIVE_REMEDIATION=true
export ARMED=0                       # Blocked at armed gate
```

### 2.2 Verify

```bash
# Plans generated (if mismatches exist)
curl -sf http://localhost:9090/metrics | grep action_planned
# Expected: > 0 if mismatches present

# Execution still zero
curl -sf http://localhost:9090/metrics | grep action_executed
# Expected: 0

# Blocked reasons visible
curl -sf http://localhost:9090/metrics | grep action_blocked
# Expected: > 0 with reason=dry_run or reason=not_armed
```

### 2.3 Pass Criteria

- [ ] `action_planned_total` > 0 (if mismatches exist)
- [ ] `action_executed_total` = 0
- [ ] `action_blocked_total` increasing with clear reasons
- [ ] Plans in audit log look reasonable
- [ ] Soak for **minimum 12 hours**

---

## Stage 3: ACTIVE (Armed, Real Orders)

**Goal:** Enable real order execution with conservative limits.

For the detailed step-by-step ACTIVE enablement ceremony with code-verified env vars, see [Runbook 22](22_ACTIVE_ENABLEMENT_CEREMONY.md).

### 3.1 Pre-Flight

- [ ] Completed Stage 1 + Stage 2 successfully
- [ ] Team monitoring in real-time
- [ ] Kill-switch drill completed (see below)
- [ ] Budget limits configured:

```bash
# Conservative first-run limits
export RECONCILE_SYMBOL_WHITELIST='["BTCUSDT"]'
export MAX_FLATTEN_NOTIONAL_USDT=100
export RECONCILE_COOLDOWN_SECONDS=60
export MAX_ORDERS_PER_ACTION=3
export MAX_SYMBOLS_PER_ACTION=1
```

### 3.2 Enable

```bash
export GRINDER_MODE=live
export RECONCILE_DRY_RUN=false
export ARMED=1
export ALLOW_MAINNET_TRADE=1
```

Restart:

```bash
docker compose restart grinder
```

### 3.3 Monitor

```bash
# Watch metrics in real-time
watch -n 5 'curl -s http://localhost:9090/metrics | grep grinder_reconcile'

# Check execution counts
curl -sf http://localhost:9090/metrics | grep action_executed
# Expected: > 0 only if mismatches detected

# Budget usage
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_budget
```

### 3.4 Pass Criteria

- [ ] `action_executed_total` increases only when mismatches detected
- [ ] Executions match planned actions
- [ ] Budget usage within limits
- [ ] No kill-switch triggers
- [ ] No unexpected errors in logs

---

## Kill-Switch Drill

Perform before Stage 3 enable. Cross-reference [Runbook 04](04_KILL_SWITCH.md) for full details.

```bash
# 1. Trigger kill-switch (simulate drawdown breach)
# Set GRINDER_KILL_SWITCH_FORCE=1 or wait for threshold breach

# 2. Verify blocks
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: grinder_kill_switch_triggered 1

# 3. Verify new orders blocked (gating_blocked_total should increase)
curl -sf http://localhost:9090/metrics | grep grinder_gating_blocked_total

# 4. Reset
# Clear GRINDER_KILL_SWITCH_FORCE, restart
docker compose restart grinder

# 5. Verify recovered
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: grinder_kill_switch_triggered 0
```

---

## Rollback Procedure

To roll back from any stage to a safer stage:

### ACTIVE → STAGING

```bash
export ARMED=0
export RECONCILE_DRY_RUN=true
unset ALLOW_MAINNET_TRADE
docker compose restart grinder
```

### STAGING → SHADOW

```bash
export RECONCILE_ACTION=none
export RECONCILE_DRY_RUN=true
docker compose restart grinder
```

### Emergency Stop

```bash
# Method 1: Disable everything
export RECONCILE_ENABLED=0
export ARMED=0

# Method 2: Full stop
docker compose stop grinder

# Verify stopped
curl -sf http://localhost:9090/metrics | grep action_executed_total
# Should stop increasing (or connection refused)
```

See [Runbook 01](01_STARTUP_SHUTDOWN.md) for full shutdown procedure.

---

## Automated Smoke Test

Run the launch readiness smoke test to validate preconditions:

```bash
# PASS demo
bash scripts/smoke_launch_readiness.sh
# Expected: EXIT 0, all checks pass

# FAIL demo (for verifying CI catches failures)
SMOKE_FORCE_FAIL=1 bash scripts/smoke_launch_readiness.sh
# Expected: EXIT 1
```

The smoke test validates:
- `/healthz` returns 200 with required JSON keys (SSOT: `REQUIRED_HEALTHZ_KEYS`)
- `/metrics` passes full contract validation (SSOT: `REQUIRED_METRICS_PATTERNS` + `FORBIDDEN_METRIC_LABELS`)
- `/readyz` responds (200 or 503) with required JSON keys (SSOT: `REQUIRED_READYZ_KEYS`)
- Graceful stop succeeds

> **Note:** Docker builder warnings (buildx/legacy builder deprecation) may appear in output. Ignore unless the exit code is non-zero.

Contract SSOT: [metrics_contract.py](../../src/grinder/observability/metrics_contract.py)

---

## Related Runbooks

| Runbook | Relevance |
|---------|-----------|
| [01 Startup/Shutdown](01_STARTUP_SHUTDOWN.md) | Start/stop commands |
| [04 Kill-Switch](04_KILL_SWITCH.md) | Kill-switch events and recovery |
| [08 Testnet Smoke](08_SMOKE_TEST_TESTNET.md) | Testnet validation before launch |
| [13 Operator Ceremony](13_OPERATOR_CEREMONY.md) | Operator ceremony for safe enablement |
| [15 Enablement Ceremony](15_ENABLEMENT_CEREMONY.md) | Staged enablement for ReconcileLoop |
| [22 ACTIVE Enablement Ceremony](22_ACTIVE_ENABLEMENT_CEREMONY.md) | Code-verified ACTIVE ceremony (Launch-02) |
