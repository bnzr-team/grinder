# Runbook 16: Reconcile Alerts and SLOs

Operational guide for reconcile-related alerts and service level objectives (LC-15b).

## Overview

This runbook covers:
- Reconcile-specific Prometheus alerts
- Service Level Objectives (SLOs) for reconciliation
- Triage procedures for each alert
- Rollback and recovery steps

---

## Alerts Reference

| Alert | Severity | Meaning |
|-------|----------|---------|
| ReconcileLoopDown | warning | Loop not running when expected |
| ReconcileSnapshotStale | warning | Snapshot age > 120s |
| ReconcileMismatchSpike | warning | High mismatch rate |
| ReconcileRemediationExecuted | critical | REAL action executed |
| ReconcileRemediationPlanned | info | Dry-run action planned |
| ReconcileRemediationBlocked | info | Action blocked by gates |
| ReconcileMismatchNoBlocks | warning | Mismatches but no remediation |
| ReconcileBudgetCallsExhausted | warning | No remediation calls remaining today |
| ReconcileBudgetNotionalLow | warning | Notional budget critically low (< $10) |

---

## Service Level Objectives (SLOs)

### SLO-1: Loop Availability

**Objective:** ReconcileLoop runs continuously when GRINDER is up.

| Metric | Target | Measurement |
|--------|--------|-------------|
| Loop run rate | > 0 runs/5min | `increase(grinder_reconcile_runs_total[5m])` |
| Availability | 99.9% | Time with runs > 0 / Total uptime |

**Alert:** ReconcileLoopDown

### SLO-2: Snapshot Freshness

**Objective:** Observed state reflects exchange within acceptable delay.

| Metric | Target | Measurement |
|--------|--------|-------------|
| Snapshot age | < 120 seconds | `grinder_reconcile_last_snapshot_age_seconds` |
| Freshness rate | 99% | Time with age < 120s / Total time |

**Alert:** ReconcileSnapshotStale

### SLO-3: Remediation Execution Budget

**Objective:** Minimize unexpected real executions during staged rollout.

| Metric | Target | Measurement |
|--------|--------|-------------|
| Executions/day | < 10 (staged rollout) | `increase(grinder_reconcile_action_executed_total[24h])` |
| Execution:Plan ratio | < 0.1 | Executed / Planned |

**Alert:** ReconcileRemediationExecuted

---

## Alert Triage Procedures

### ReconcileLoopDown

**Severity:** Warning

**Meaning:** No reconcile loop runs detected in 5 minutes while GRINDER is up.

**Impact:** State drift will not be detected or remediated.

**Triage Steps:**

1. Check if RECONCILE_ENABLED=1:
   ```bash
   echo $RECONCILE_ENABLED
   ```

2. Check for errors in logs:
   ```bash
   grep -E "RECONCILE|ERROR" /var/log/grinder/app.log | tail -20
   ```

3. Check ReconcileLoop status in metrics:
   ```bash
   curl -s http://localhost:9090/metrics | grep grinder_reconcile
   ```

4. Verify data sources are available:
   ```bash
   # REST snapshot
   curl -s http://localhost:9090/metrics | grep last_snapshot_age
   ```

**Resolution:**
- If RECONCILE_ENABLED=0: Enable if intended, or silence alert
- If errors: Check exchange connectivity, credentials
- If stuck: Restart GRINDER gracefully

---

### ReconcileSnapshotStale

**Severity:** Warning

**Meaning:** Snapshot age exceeds 120 seconds threshold.

**Impact:** Reconciliation decisions based on stale data; may miss mismatches or act on outdated state.

**Triage Steps:**

1. Check current snapshot age:
   ```bash
   curl -s http://localhost:9090/metrics | grep last_snapshot_age
   ```

2. Check REST API connectivity:
   ```bash
   # Verify exchange REST endpoint
   curl -I https://fapi.binance.com/fapi/v1/ping
   ```

3. Check for rate limiting:
   ```bash
   grep -E "429|RATE_LIMIT" /var/log/grinder/app.log | tail -10
   ```

4. Check graceful degradation:
   ```bash
   grep "Skipping" /var/log/grinder/app.log | tail -10
   ```

**Resolution:**
- If network issue: Check connectivity, DNS, firewall
- If rate limited: Reduce snapshot frequency or wait
- If API down: Loop will continue with graceful degradation

---

### ReconcileMismatchSpike

**Severity:** Warning

**Meaning:** High rate of detected mismatches (> 0.1/sec for 3 minutes).

**Impact:** Possible state divergence; many unexpected orders/positions.

**Triage Steps:**

1. Check mismatch types:
   ```bash
   curl -s http://localhost:9090/metrics | grep mismatch_total
   ```

2. Review recent reconcile reports:
   ```bash
   grep "MISMATCH" /var/log/grinder/app.log | tail -20
   ```

3. Check if manual trading occurred:
   ```bash
   # Orders without grinder_ prefix
   grep "unexpected" /var/log/grinder/app.log | tail -10
   ```

4. Verify identity config:
   ```bash
   grep -E "prefix|allowed_strategies" config.yaml
   ```

**Resolution:**
- If manual orders: Expected - will be handled by remediation
- If strategy mismatch: Check strategy ID configuration
- If persistent: Review ReconcileEngine configuration

---

### ReconcileRemediationExecuted

**Severity:** Critical

**Meaning:** A REAL remediation action (cancel/flatten) was executed.

**Impact:** Live trade execution - verify it was intended and correct.

**Triage Steps:**

1. **IMMEDIATELY** verify in audit log:
   ```bash
   grep "EXECUTED" audit/reconcile.jsonl | tail -5
   ```

2. Check exchange for actual orders:
   ```bash
   # Verify on Binance
   # Check open orders / recent trades
   ```

3. Verify gates were configured correctly:
   ```bash
   echo "ARMED=$ARMED"
   echo "ALLOW_MAINNET_TRADE=$ALLOW_MAINNET_TRADE"
   echo "RECONCILE_DRY_RUN=$RECONCILE_DRY_RUN"
   ```

4. Check what was executed:
   ```bash
   curl -s http://localhost:9090/metrics | grep action_executed
   ```

**Resolution:**
- If intended: Document and update runbook
- If unintended: **IMMEDIATELY** set RECONCILE_ENABLED=0 or ARMED=0
- Review and tighten gate configuration

**Rollback:**
```bash
# Immediate stop
export RECONCILE_ENABLED=0
# Or: export ARMED=0
# Or: export RECONCILE_DRY_RUN=true
```

---

### ReconcileRemediationPlanned

**Severity:** Info

**Meaning:** A remediation action was planned in dry-run mode.

**Impact:** None - action was simulated, not executed.

**Triage Steps:**

1. Review planned actions:
   ```bash
   grep "PLANNED" audit/reconcile.jsonl | tail -10
   ```

2. Verify this is expected for staged rollout:
   ```bash
   echo "RECONCILE_DRY_RUN=$RECONCILE_DRY_RUN"
   ```

**Resolution:**
- Expected behavior during Stage 2 (Plan-only)
- Review plans before enabling execution

---

### ReconcileRemediationBlocked

**Severity:** Info

**Meaning:** Remediation was blocked by one or more safety gates.

**Impact:** None - gates working as designed.

**Triage Steps:**

1. Check blocked reasons:
   ```bash
   curl -s http://localhost:9090/metrics | grep action_blocked
   ```

2. Verify expected blocking reason:
   ```bash
   # During staged rollout, expect: not_armed, dry_run, etc.
   grep "BLOCKED" audit/reconcile.jsonl | tail -5
   ```

**Resolution:**
- Expected behavior during Stage 3 (Blocked)
- If you want to execute: enable gates per ceremony

---

### ReconcileMismatchNoBlocks

**Severity:** Warning

**Meaning:** Mismatches detected but no remediation planned or blocked.

**Impact:** State drift exists but no action is being taken.

**Triage Steps:**

1. Check RECONCILE_ACTION setting:
   ```bash
   echo $RECONCILE_ACTION
   # Expected: none (detect-only), cancel_all, flatten, auto
   ```

2. If action=none, this is expected in Stage 1 (Detect-only)

3. If action != none, check for errors:
   ```bash
   grep "REMEDIAT" /var/log/grinder/app.log | tail -10
   ```

**Resolution:**
- If detect-only: Expected behavior, silence if not needed
- If remediation expected: Check RemediationExecutor configuration

---

### ReconcileBudgetCallsExhausted

**Severity:** Warning

**Meaning:** No remediation calls remaining for today (budget exhausted).

**Impact:** Any new mismatches will be detected but NOT remediated until budget resets.

**Note:** This alert only fires when `grinder_reconcile_budget_configured=1`.
If budget tracking is not active (no RemediationExecutor initialized), the alert is suppressed.

**Triage Steps:**

1. Check budget state:
   ```bash
   curl -s http://localhost:9090/metrics | grep grinder_reconcile_budget
   ```

2. Verify calls used:
   ```bash
   # Check audit log for today's executions
   grep "EXECUTED" audit/reconcile.jsonl | wc -l
   ```

3. Review if budget was intentionally tight:
   ```bash
   echo "MAX_CALLS_PER_DAY=$MAX_CALLS_PER_DAY"
   echo "MAX_CALLS_PER_RUN=$MAX_CALLS_PER_RUN"
   ```

4. Check budget state file:
   ```bash
   cat $BUDGET_STATE_PATH
   ```

**Resolution:**
- If expected (staged rollout): Wait for daily budget reset (UTC midnight)
- If urgent: Reset budget with `--reset-budget-state` flag (see M4.2)
- If budget too tight: Increase `MAX_CALLS_PER_DAY` and restart
- Review executed actions: Were they all necessary?

**Budget Reset:**
```bash
# Option 1: Wait for automatic daily reset (UTC midnight)
# Option 2: Manual reset (use with caution!)
rm $BUDGET_STATE_PATH
# Or use the CLI flag:
PYTHONPATH=src python3 -m scripts.run_live_reconcile --reset-budget-state ...
```

---

### ReconcileBudgetNotionalLow

**Severity:** Warning

**Meaning:** Remaining notional budget is critically low (< $10 USDT).

**Impact:** Only very small remediation actions can execute; larger positions may not be flattened.

**Note:** This alert only fires when `grinder_reconcile_budget_configured=1`.
If budget tracking is not active (no RemediationExecutor initialized), the alert is suppressed.

**Triage Steps:**

1. Check current budget state:
   ```bash
   curl -s http://localhost:9090/metrics | grep grinder_reconcile_budget
   ```

2. Review notional usage:
   ```bash
   # Check what notional was used
   grep "EXECUTED.*notional" audit/reconcile.jsonl | tail -10
   ```

3. Verify budget configuration:
   ```bash
   echo "MAX_NOTIONAL_PER_DAY=$MAX_NOTIONAL_PER_DAY"
   echo "FLATTEN_MAX_NOTIONAL_PER_CALL=$FLATTEN_MAX_NOTIONAL_PER_CALL"
   ```

**Resolution:**
- If expected (tight budgets for testing): Wait for daily reset or adjust limits
- If urgent remediation needed:
  1. Reset budget state (see ReconcileBudgetCallsExhausted)
  2. Or increase `MAX_NOTIONAL_PER_DAY` and restart
- Review if positions are larger than expected

**See Also:**
- [Runbook 13: Stage E](13_OPERATOR_CEREMONY.md#stage-e-non-btc-symbol-micro-test-flatten) - Non-BTC micro-test with tight budgets
- M4.2 BudgetState Policy for budget lifecycle management

---

## Emergency Rollback

### Immediate Stop (Big Red Button)

```bash
# Method 1: Disable loop entirely
export RECONCILE_ENABLED=0

# Method 2: Disable remediation but keep monitoring
export RECONCILE_ACTION=none

# Method 3: Block execution via armed
export ARMED=0

# Method 4: Enable dry-run
export RECONCILE_DRY_RUN=true
```

### Verify Rollback

```bash
# Check no new executions
BEFORE=$(curl -s http://localhost:9090/metrics | grep action_executed_total | awk '{print $2}')
sleep 60
AFTER=$(curl -s http://localhost:9090/metrics | grep action_executed_total | awk '{print $2}')
[ "$BEFORE" = "$AFTER" ] && echo "STOPPED OK" || echo "WARNING: Still executing"
```

---

## Grafana Dashboard Queries

### Reconcile Overview Panel

```promql
# Loop runs per minute
rate(grinder_reconcile_runs_total[1m]) * 60

# Mismatch rate
sum(rate(grinder_reconcile_mismatch_total[5m]))

# Snapshot age
grinder_reconcile_last_snapshot_age_seconds
```

### Remediation Panel

```promql
# Actions planned (dry-run)
sum(increase(grinder_reconcile_action_planned_total[1h]))

# Actions executed (real)
sum(increase(grinder_reconcile_action_executed_total[1h]))

# Actions blocked
sum(increase(grinder_reconcile_action_blocked_total[1h])) by (reason)
```

### SLO Burn Rate

```promql
# Loop availability (should be > 0)
increase(grinder_reconcile_runs_total[5m]) > 0

# Snapshot freshness (should be 1)
grinder_reconcile_last_snapshot_age_seconds < 120
```

### Budget Panel (LC-18)

```promql
# Calls remaining today (red=0, yellow=1-4, green=5+)
grinder_reconcile_budget_calls_remaining_day

# Calls used today
grinder_reconcile_budget_calls_used_day

# Notional remaining today (red=0, yellow=<$10, green=$50+)
grinder_reconcile_budget_notional_remaining_day

# Notional used today
grinder_reconcile_budget_notional_used_day
```

---

## Related Documentation

- [Runbook 11](11_RECONCILIATION_TRIAGE.md): Reconciliation Triage
- [Runbook 12](12_ACTIVE_REMEDIATION.md): Active Remediation
- [Runbook 13](13_OPERATOR_CEREMONY.md): Operator Ceremony
- [Runbook 15](15_ENABLEMENT_CEREMONY.md): Enablement Ceremony
- [ADR-051](../DECISIONS.md#adr-051): Reconcile Alerts and SLOs
