# Runbook 15: ReconcileLoop Staged Enablement Ceremony

Safe, staged procedure for enabling ReconcileLoop in production (LC-15a).

## Overview

This runbook provides a **step-by-step ceremony** for enabling reconciliation with remediation in production. Each stage has explicit verification criteria and rollback steps.

**Stages:**
0. Baseline (disabled)
1. Detect-only (observe, no actions)
2. Plan-only (dry-run remediation)
3. Execution blocked (verify gates)
4. Live execution (limited blast radius)

## Prerequisites Checklist

Before starting the ceremony, verify:

- [ ] **Identity config**: prefix and strategy allowlist configured
  ```bash
  # Verify identity config
  grep -E "prefix|allowed_strategies" config.yaml
  # Expected: prefix: "grinder_", allowed_strategies: ["1", "2", ...]
  ```

- [ ] **Symbol whitelist**: only symbols you intend to trade
  ```bash
  # Example: ["BTCUSDT", "ETHUSDT"]
  echo $RECONCILE_SYMBOL_WHITELIST
  ```

- [ ] **Notional caps**: conservative limits for first enablement
  ```bash
  # max_flatten_notional_usdt <= 100 for first run
  echo $MAX_FLATTEN_NOTIONAL_USDT
  ```

- [ ] **Cooldown**: non-zero to prevent rapid-fire
  ```bash
  # cooldown_seconds >= 60
  echo $RECONCILE_COOLDOWN_SECONDS
  ```

- [ ] **Audit enabled** (recommended):
  ```bash
  # GRINDER_AUDIT_ENABLED=1
  echo $GRINDER_AUDIT_ENABLED
  ```

- [ ] **Team aware**: Notify team of go-live window

---

## Stage 0: Baseline (Disabled)

Starting point: everything disabled, verify clean state.

### 0.1 Configuration

```bash
# Environment
export RECONCILE_ENABLED=0
export RECONCILE_ACTION=none
export ARMED=0
unset ALLOW_MAINNET_TRADE
```

### 0.2 Verification

```bash
# Loop should not be running
curl -s http://localhost:9090/metrics | grep grinder_reconcile_runs_total
# Expected: 0 or not present

# No port calls
grep "FAKE_PORT\|place_market_order\|cancel_order" /var/log/grinder/app.log | wc -l
# Expected: 0
```

### 0.3 Pass Criteria

- [ ] `RECONCILE_ENABLED=0`
- [ ] No reconcile runs in metrics
- [ ] No execution calls in logs

---

## Stage 1: Detect-Only Loop

Enable loop but **no remediation actions**. Observe state and verify data sources.

### 1.1 Configuration

```bash
export RECONCILE_ENABLED=1
export RECONCILE_INTERVAL_MS=30000
export RECONCILE_ACTION=none       # No remediation
export RECONCILE_DRY_RUN=true      # Extra safety
```

### 1.2 Start and Monitor

```bash
# Start application with ReconcileLoop
./run_live.sh

# Wait 2-3 intervals, then check metrics
sleep 90
curl -s http://localhost:9090/metrics | grep grinder_reconcile

# Expected:
# grinder_reconcile_runs_total > 0
# grinder_reconcile_runs_with_mismatch_total >= 0
# grinder_reconcile_action_executed_total = 0
```

### 1.3 Verify Data Sources

```bash
# Check snapshot freshness
curl -s http://localhost:9090/metrics | grep last_snapshot_age
# Expected: < 120 seconds

# Check for graceful degradation (OK if some sources unavailable)
grep -E "REST: Skipping|WS: Skipping|PRICE: Fetch" /var/log/grinder/app.log | tail -5
```

### 1.4 Verify Zero Execution

```bash
# CRITICAL: Must be zero
curl -s http://localhost:9090/metrics | grep action_executed
# Expected: 0 or not present

grep -E "REMEDIATION_EXECUTED|port\.cancel|port\.place" /var/log/grinder/app.log | wc -l
# Expected: 0
```

### 1.5 Pass Criteria

- [ ] `runs_total` increasing every interval
- [ ] `action_executed_total` = 0
- [ ] `last_snapshot_age_seconds` < 120 (or graceful degradation)
- [ ] No execution calls in logs
- [ ] Run for **minimum 10 minutes** before proceeding

---

## Stage 2: Plan-Only (Dry-Run)

Enable remediation planning, but **don't execute**. Verify plans look correct.

### 2.1 Configuration

```bash
export RECONCILE_ACTION=cancel_all   # Or: flatten, auto
export RECONCILE_DRY_RUN=true        # Plans only, no execution
export ALLOW_ACTIVE_REMEDIATION=true # Allow planning
export ARMED=0                       # Blocked at armed gate
```

### 2.2 Inject Test Mismatch (Optional)

To verify planning works, inject a controlled mismatch:

```bash
# Using smoke script
PYTHONPATH=src python3 -m scripts.smoke_enablement_ceremony --inject-mismatch
```

### 2.3 Verify Plans

```bash
# Check planned actions
curl -s http://localhost:9090/metrics | grep action_planned
# Expected: > 0 if mismatch exists

# Check audit log for PLANNED status
grep "PLANNED" audit/reconcile.jsonl | tail -5
```

### 2.4 Verify Zero Execution

```bash
# CRITICAL: Still zero
curl -s http://localhost:9090/metrics | grep action_executed
# Expected: 0

# Blocked reasons should show
curl -s http://localhost:9090/metrics | grep action_blocked
# Expected: > 0 with reason=dry_run or reason=not_armed
```

### 2.5 Pass Criteria

- [ ] `action_planned_total` > 0 (if mismatch exists)
- [ ] `action_executed_total` = 0
- [ ] `action_blocked_total{reason="dry_run"}` > 0 or `reason="not_armed"`
- [ ] Plans in audit log look correct
- [ ] Run for **minimum 10 minutes** before proceeding

---

## Stage 3: Execution Blocked (Verify Gates)

Disable dry_run, but keep gates blocked. Verify correct blocking reasons.

### 3.1 Configuration

```bash
export RECONCILE_DRY_RUN=false       # No longer dry-run
export ALLOW_ACTIVE_REMEDIATION=true # Allow remediation
export ARMED=0                       # BUT: Still blocked at armed gate
unset ALLOW_MAINNET_TRADE            # AND: No mainnet trade permission
```

### 3.2 Verify Blocking

```bash
# Should see blocked with specific reasons
curl -s http://localhost:9090/metrics | grep action_blocked

# Expected reasons (any of):
# reason="not_armed"
# reason="env_var_missing"
```

### 3.3 Verify Zero Execution

```bash
# CRITICAL: Still zero
curl -s http://localhost:9090/metrics | grep action_executed
# Expected: 0

grep "REMEDIATION_EXECUTED" /var/log/grinder/app.log | wc -l
# Expected: 0
```

### 3.4 Pass Criteria

- [ ] `action_blocked_total` increasing with clear reasons
- [ ] `action_executed_total` = 0
- [ ] Blocking reason matches expected gate
- [ ] No execution calls in logs

---

## Stage 4: Live Execution (Limited Blast Radius)

Enable real execution with **conservative limits**.

### 4.1 Pre-Flight Checklist

Before proceeding:
- [ ] Completed stages 0-3 successfully
- [ ] Team notified and monitoring
- [ ] Symbol whitelist is minimal (1-2 symbols)
- [ ] Notional cap is low (e.g., $100)
- [ ] Cooldown is non-zero (e.g., 60s)

### 4.2 Configuration

```bash
# Final gates
export ARMED=1
export ALLOW_MAINNET_TRADE=1

# Limits (conservative!)
export RECONCILE_SYMBOL_WHITELIST='["BTCUSDT"]'
export MAX_FLATTEN_NOTIONAL_USDT=100
export RECONCILE_COOLDOWN_SECONDS=60
export MAX_ORDERS_PER_ACTION=3
export MAX_SYMBOLS_PER_ACTION=1
```

### 4.3 Monitor Execution

```bash
# Watch metrics in real-time
watch -n 5 'curl -s http://localhost:9090/metrics | grep grinder_reconcile'

# Check for executions
curl -s http://localhost:9090/metrics | grep action_executed
# Expected: > 0 only if mismatches detected

# Verify audit
tail -f audit/reconcile.jsonl | grep EXECUTED
```

### 4.4 Success Criteria

- [ ] `action_executed_total` increases **only when mismatch exists**
- [ ] Executions match planned actions
- [ ] Audit shows EXECUTED with correct details
- [ ] No unexpected errors

---

## Stop / Rollback

If anything goes wrong, use these steps to immediately stop.

### Immediate Stop (Big Red Button)

```bash
# Method 1: Disable loop entirely
export RECONCILE_ENABLED=0

# Method 2: Disable remediation but keep monitoring
export RECONCILE_ACTION=none

# Method 3: Block execution via armed
export ARMED=0

# Verify stopped
curl -s http://localhost:9090/metrics | grep action_executed
# Should stop increasing
```

### Verify Rollback

```bash
# Check no new executions
BEFORE=$(curl -s http://localhost:9090/metrics | grep action_executed_total | awk '{print $2}')
sleep 60
AFTER=$(curl -s http://localhost:9090/metrics | grep action_executed_total | awk '{print $2}')
[ "$BEFORE" = "$AFTER" ] && echo "STOPPED OK" || echo "WARNING: Still executing"
```

### Kill-Switch Semantics

Note: Kill-switch (if triggered) **allows** remediation actions:
- Remediation reduces risk by closing unexpected positions
- Kill-switch blocks new trades, but cancels/flattens are allowed
- This is intentional safety behavior (ADR-043)

---

## Failure Modes Checklist

| Failure | Expected Behavior | Action |
|---------|-------------------|--------|
| Snapshot unavailable | Graceful degradation, loop continues | Check REST connectivity |
| WS unavailable | Graceful degradation, loop continues | Check WS connectivity |
| Price getter None | Flatten blocked (notional unknown) | Check Binance REST API |
| All gates blocked | No execution, logs reason | Verify configuration |
| Unexpected execution | Should not happen if gates configured | Rollback immediately |

---

## Operator Quick Reference

```bash
# Start detect-only
RECONCILE_ENABLED=1 RECONCILE_ACTION=none ./run_live.sh

# Enable planning (dry-run)
RECONCILE_ACTION=cancel_all RECONCILE_DRY_RUN=true ./run_live.sh

# Enable execution (with gates)
ARMED=1 ALLOW_MAINNET_TRADE=1 RECONCILE_DRY_RUN=false ./run_live.sh

# Emergency stop
export RECONCILE_ENABLED=0  # or: export RECONCILE_ACTION=none

# Verify stopped
curl -s localhost:9090/metrics | grep action_executed
```

---

## Related Documentation

- [ADR-048](../DECISIONS.md#adr-048): ReconcileLoop Wiring
- [ADR-049](../DECISIONS.md#adr-049): Real Sources Wiring
- [ADR-050](../DECISIONS.md#adr-050): Operator Ceremony
- [Runbook 11](11_RECONCILIATION_TRIAGE.md): Reconciliation Triage
- [Runbook 12](12_ACTIVE_REMEDIATION.md): Active Remediation
- [Runbook 13](13_OPERATOR_CEREMONY.md): Remediation Ceremony
