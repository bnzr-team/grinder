# Runbook 22: ACTIVE Enablement Ceremony (Launch-02)

Step-by-step operator ceremony for safely flipping GRINDER to ACTIVE on a single venue (Binance USDT-M).

## 0. Purpose and Scope

This runbook covers the **critical moment** of transitioning from STAGING to ACTIVE — the point where real orders hit the exchange. Every environment variable listed here is code-verified with `Source:` annotations pointing to the exact file and line where it is consumed.

**Principles:** bounded rollout, fail-closed defaults, fast rollback (<5 min), kill-switch drill mandatory before go-live.

**Prerequisites:** SHADOW (>=24h) + STAGING (>=12h) completed per [Runbook 21](21_SINGLE_VENUE_LAUNCH.md).

> Paths in this runbook (e.g., `artifacts/`, `audit/reconcile.jsonl`) depend on container WORKDIR and compose volume mapping. Verify with the permissions check command in the preconditions below.

---

## 1. Preconditions

Complete **all** items before proceeding to the ceremony:

- [ ] **CI green** + `make gates` PASS on current `main`
- [ ] **Launch readiness smoke** PASS on target machine: `bash scripts/smoke_launch_readiness.sh`
- [ ] **Observability stack** accessible (Grafana, Prometheus — ports per `docker-compose.yml` / `docker-compose.observability.yml`)
- [ ] **Kill-switch drill** passed (see [Section 7](#7-kill-switch-drill-mandatory-before-section-5))
- [ ] **Rollback verification** passed (see [Section 8](#8-rollback-active--staging-in-5-min))
- [ ] **API credentials** set: `BINANCE_API_KEY` + `BINANCE_API_SECRET`
  - Source: `scripts/run_live_reconcile.py:379-380`
- [ ] **Audit enabled**: `GRINDER_AUDIT_ENABLED=1`, `GRINDER_ARTIFACTS_DIR` configured
- [ ] **Budget state**: `BUDGET_STATE_PATH` set, file fresh or run with `--reset-budget-state`
- [ ] **Container permissions verified**:
  ```bash
  docker compose exec grinder sh -lc 'id && ls -ld /app/artifacts /app/audit || true'
  ```
- [ ] **Operator assigned** + time window agreed (15-30 min for first ACTIVE run)

---

## 2. Stage Definitions (SSOT)

Stages are controlled by a **single lever**: `REMEDIATION_MODE`.

| Stage | `REMEDIATION_MODE` | What happens |
|-------|-------------------|--------------|
| **SHADOW** | `detect_only` | Loop runs, snapshots taken. No plans, no actions. |
| **STAGING** | `plan_only` | Plans generated for detected mismatches. Not executed. |
| **ACTIVE** | `execute_cancel_all` | First window. Plans executed: cancels stale orders. |
| **ACTIVE+** | `execute_flatten` | Later graduation. Also flattens unexpected positions. |

`REMEDIATION_MODE` is the SSOT lever. The internal flags `dry_run`, `allow_active_remediation`, and `armed` are **auto-derived** from it (`scripts/run_live_reconcile.py:234-246,773`). Do not set them manually for the ceremony.

For full stage overview and minimum soak durations, see [Runbook 21](21_SINGLE_VENUE_LAUNCH.md).

---

## 3. Conservative ACTIVE Configuration (First Window)

Every env var below is **verified wired in code**. `Source:` comments point to where the value is consumed.

### Trading authorization

```bash
export ALLOW_MAINNET_TRADE=1
# Source: src/grinder/reconcile/remediation.py:223 (Gate 5)
#         src/grinder/execution/binance_futures_port.py:133 (port init guard)
#         src/grinder/connectors/live_connector.py:544 (LC-22 gate)
#         scripts/run_live_reconcile.py:276 (startup validation)
```

### Reconcile loop

```bash
export RECONCILE_ENABLED=1
# Source: src/grinder/live/reconcile_loop.py:75

export REMEDIATION_MODE=execute_cancel_all
# Source: scripts/run_live_reconcile.py:197
# Auto-derives: dry_run=False, allow_active=True, armed=True (lines 235-238, 773)
```

### Budget limits (first-window overrides, tighter than defaults)

```bash
export MAX_CALLS_PER_DAY=20
# Source: scripts/run_live_reconcile.py:207 (default=100)

export MAX_NOTIONAL_PER_DAY=1000
# Source: scripts/run_live_reconcile.py:212 (default=5000 USDT)

export MAX_CALLS_PER_RUN=3
# Source: scripts/run_live_reconcile.py:217 (default=10)

export MAX_NOTIONAL_PER_RUN=200
# Source: scripts/run_live_reconcile.py:222 (default=1000 USDT)

export FLATTEN_MAX_NOTIONAL_PER_CALL=100
# Source: scripts/run_live_reconcile.py:227 (default=500 USDT)
```

### Scope limits

```bash
export REMEDIATION_SYMBOL_ALLOWLIST=BTCUSDT
# Source: scripts/run_live_reconcile.py:202
```

### Audit and artifacts

```bash
export GRINDER_AUDIT_ENABLED=1
# Source: src/grinder/reconcile/audit.py:143

export GRINDER_ARTIFACTS_DIR=artifacts/
# Source: src/grinder/ops/artifacts.py:112 (via ENV_ARTIFACTS_DIR const, line 29)

export BUDGET_STATE_PATH=artifacts/budget.json
# Source: scripts/run_live_reconcile.py:232
```

### Notes on derived / non-wired variables

- **`GRINDER_MODE`**: Compose label for human readability only. NOT consumed by Python code. Set to `live` as a hint.
- **`ARMED`**: Auto-derived from `REMEDIATION_MODE` (`armed=not config.dry_run`, `run_live_reconcile.py:773`). Only set explicitly for smoke scripts (`smoke_reconcile_e2e.py`).
- **`RECONCILE_DRY_RUN`**, **`RECONCILE_ALLOW_ACTIVE`**: Auto-derived from `REMEDIATION_MODE` (`run_live_reconcile.py:237-245`). Only set explicitly for smoke scripts.

---

## 4. Preflight Commands

Run these **before** flipping to ACTIVE. Expected output noted.

```bash
# 1. Health check
curl -sf http://localhost:9090/healthz | python3 -m json.tool
# Expected: {"status": "ok", "uptime_s": ...}

# 2. Metrics alive
curl -sf http://localhost:9090/metrics | grep grinder_up
# Expected: grinder_up 1

# 3. Kill-switch NOT triggered
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: grinder_kill_switch_triggered 0

# 4. Readiness (200=HA-active, 503=non-HA/standby — both OK)
curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/readyz
# Expected: 200 or 503

# 5. Verify current stage is STAGING
echo $REMEDIATION_MODE
# Expected: plan_only

# 6. Launch readiness smoke
bash scripts/smoke_launch_readiness.sh
# Expected: EXIT 0
```

---

## 5. Flip to ACTIVE (The Ceremony)

### 5.1 Record starting state

Save a ceremony log before making any changes:

```bash
echo "=== CEREMONY LOG ==="
echo "Operator: $(whoami)"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Git SHA: $(git rev-parse HEAD)"
echo ""
echo "=== ENV SNAPSHOT ==="
env | grep -E "GRINDER_|ALLOW_|RECONCILE_|REMEDIATION_|MAX_|BUDGET_|BINANCE_API_KEY" | sort
# Note: do NOT log BINANCE_API_SECRET in plaintext
```

Save this output to `artifacts/ceremony_$(date -u +%Y%m%d).log`.

### 5.2 Set ACTIVE env vars

Apply the configuration from [Section 3](#3-conservative-active-configuration-first-window).

### 5.3 Restart

```bash
docker compose restart grinder
```

See [Runbook 01](01_STARTUP_SHUTDOWN.md) for full startup/shutdown procedure.

### 5.4 First 1-3 minutes verification

```bash
# Health
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# Reconcile loop running
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total
# Expected: incrementing

# Kill-switch clean
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: 0

# No unexpected errors
docker compose logs grinder --tail=50 2>&1 | grep -i error || echo "No errors"
```

---

## 6. ACTIVE Hold Period

Minimum: **30 minutes** for the first ACTIVE window.

### Metric watchlist

Monitor these continuously during the hold period:

| Metric | Expected | Red flag |
|--------|----------|----------|
| `grinder_kill_switch_triggered` | 0 | 1 = immediate rollback |
| `grinder_drawdown_pct` | < configured threshold | Approaching threshold |
| `grinder_reconcile_budget_calls_remaining_day` | > 0 | 0 = budget exhausted |
| `grinder_reconcile_budget_notional_remaining_day` | > 0 | 0 = budget exhausted |
| `grinder_gating_blocked_total{reason=...}` | `rate_limit`, `cooldown` OK | `KILL_SWITCH_ACTIVE` = red |
| `grinder_circuit_state{state="open"}` | 0 | > 0 = exchange connectivity issue |
| `grinder_reconcile_action_blocked_total` | 0 (gates are open) | Unexpected blocking reasons |

Quick monitoring one-liner:

```bash
watch -n 5 'curl -sf http://localhost:9090/metrics | grep -E "kill_switch_triggered|drawdown_pct|budget_(calls|notional)_remaining|circuit_state.*open|action_(executed|blocked)_total"'
```

### First execution verification

When `grinder_reconcile_action_executed_total` first increments:

1. Open Binance Futures web UI or use the API to verify the expected action occurred
2. Match the execution against the audit log:
   ```bash
   tail -5 audit/reconcile.jsonl | python3 -m json.tool
   ```
3. Confirm the action matches what was planned

---

## 7. Kill-Switch Drill (Mandatory Before Section 5)

**Run this drill in STAGING (`plan_only`) before going ACTIVE.** No real orders are sent in STAGING.

Cross-reference [Runbook 04](04_KILL_SWITCH.md) for full kill-switch details.

The kill-switch is an **in-process latch** (`src/grinder/risk/kill_switch.py:123`). There is no env var or HTTP endpoint to reset it. The only reset path is **container restart**.

### Drill steps

```bash
# 1. Trigger kill-switch via smoke script (in STAGING)
python3 -m scripts.smoke_live_testnet --kill-switch

# 2. Verify triggered
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: grinder_kill_switch_triggered 1

# 3. Verify gating blocks
curl -sf http://localhost:9090/metrics | grep 'gating_blocked_total.*KILL_SWITCH'
# Expected: grinder_gating_blocked_total{...reason="KILL_SWITCH_ACTIVE"...} incrementing

# 4. Reset (only path: restart)
docker compose restart grinder

# 5. Verify recovered
sleep 10
curl -sf http://localhost:9090/metrics | grep grinder_kill_switch_triggered
# Expected: grinder_kill_switch_triggered 0
```

- [ ] Drill passed: kill-switch triggers, blocks, resets correctly

---

## 8. Rollback (ACTIVE -> STAGING in <5 min)

### Conditions triggering rollback

- `grinder_kill_switch_triggered` = 1 (unexpected)
- `grinder_drawdown_pct` approaching or exceeding threshold
- Unexpected spike in `grinder_reconcile_action_executed_total`
- `grinder_circuit_state{state="open"}` > 0 (exchange connectivity)
- **Any operator doubt** — when in doubt, roll back

### Rollback steps

```bash
export REMEDIATION_MODE=detect_only
unset ALLOW_MAINNET_TRADE
docker compose restart grinder
```

### Rollback verification signals

All three must be true after rollback:

1. `grinder_reconcile_action_blocked_total{reason="dry_run"}` incrementing
   (Gates 2/3/4 block because `REMEDIATION_MODE=detect_only` sets `dry_run=True`)
2. `grinder_reconcile_action_blocked_total{reason="env_var_missing"}` incrementing
   (Gate 5 blocks because `ALLOW_MAINNET_TRADE` is unset — `remediation.py:428`)
3. `grinder_reconcile_action_executed_total` stays zero over 60 seconds

```bash
# Verify signals
BEFORE=$(curl -sf http://localhost:9090/metrics | grep 'action_executed_total' | awk '{print $2}')
sleep 60
AFTER=$(curl -sf http://localhost:9090/metrics | grep 'action_executed_total' | awk '{print $2}')
[ "$BEFORE" = "$AFTER" ] && echo "ROLLBACK OK: executed_total stable" || echo "WARNING: still executing"

# Verify block reasons
curl -sf http://localhost:9090/metrics | grep 'action_blocked_total'
# Expected: reason="dry_run" and reason="env_var_missing" present
```

### Rollback verification test (run BEFORE Section 5)

1. In STAGING (`REMEDIATION_MODE=plan_only`), start grinder
2. Wait for `grinder_reconcile_runs_total` > 0
3. Execute the rollback steps above
4. Verify all three signals
5. Verify `/healthz` still returns 200

- [ ] Rollback verification test passed

---

## 9. Exit Criteria (Declare Success)

- [ ] ACTIVE window lasted >= 30 minutes
- [ ] No kill-switch trips
- [ ] No unexpected drawdown
- [ ] Budget usage within limits (`calls_remaining` > 0, `notional_remaining` > 0)
- [ ] No circuit breaker trips
- [ ] First execution verified on exchange (if any occurred)
- [ ] Metrics stable throughout hold period

### Post-ceremony artifacts

Save to `artifacts/ceremony_<date>/`:

```bash
CEREMONY_DIR="artifacts/ceremony_$(date -u +%Y%m%d)"
mkdir -p "$CEREMONY_DIR"

# Env snapshot (exclude secrets)
env | grep -E "GRINDER_|ALLOW_|RECONCILE_|REMEDIATION_|MAX_|BUDGET_" | sort > "$CEREMONY_DIR/env.txt"

# Metrics snapshot
curl -sf http://localhost:9090/metrics > "$CEREMONY_DIR/metrics_at_exit.prom"

# Container logs
docker compose logs grinder --no-color > "$CEREMONY_DIR/grinder.log"

# Audit log copy
cp audit/reconcile.jsonl "$CEREMONY_DIR/" 2>/dev/null || echo "No audit log"
```

---

## 10. Limit Graduation (Post First ACTIVE)

After a successful first ACTIVE window, gradually increase limits week by week:

| Week | `MAX_CALLS_PER_RUN` | Symbols | `MAX_NOTIONAL_PER_DAY` |
|------|---------------------|---------|------------------------|
| 1 | 3 | BTCUSDT | 1000 |
| 2 | 5 | BTCUSDT | 2000 |
| 3 | 10 | BTCUSDT, ETHUSDT | 3000 |
| 4 | 10 | +SOLUSDT | 5000 (default) |

Each graduation step should be preceded by a review of the previous period's metrics and audit logs. See [Runbook 13](13_OPERATOR_CEREMONY.md) for the detailed graduation procedure.

---

## Related Runbooks

| Runbook | Relevance |
|---------|-----------|
| [01 Startup/Shutdown](01_STARTUP_SHUTDOWN.md) | Start/stop commands |
| [04 Kill-Switch](04_KILL_SWITCH.md) | Kill-switch events, drill, and recovery |
| [13 Operator Ceremony](13_OPERATOR_CEREMONY.md) | Detailed graduation procedure |
| [15 Enablement Ceremony](15_ENABLEMENT_CEREMONY.md) | ReconcileLoop staged enablement |
| [21 Single-Venue Launch](21_SINGLE_VENUE_LAUNCH.md) | Stage overview and launch readiness |
