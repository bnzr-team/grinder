# Runbook 13: Operator Ceremony for Active Remediation

Safe enablement procedure for active reconciliation remediation (LC-11).

## Overview

This runbook guides you through the staged enablement of active remediation:
1. Verify passive reconciliation is healthy
2. Enable dry-run mode to observe plans
3. Gradually enable real execution with minimal limits
4. Monitor metrics and adjust limits

## Prerequisites

- [ ] LC-10 active remediation module deployed
- [ ] LC-11 runner wiring deployed
- [ ] Passive reconciliation (LC-09b) running for 24+ hours
- [ ] No unresolved mismatches in logs
- [ ] Team aware of go-live schedule

## Stage 1: Verify Passive Health

Before enabling any active features, verify passive reconciliation is healthy.

### 1.1 Check Reconciliation Metrics

```bash
curl -s http://localhost:9090/metrics | grep grinder_reconcile

# Expected (healthy state):
# grinder_reconcile_runs_total 500+
# grinder_reconcile_last_snapshot_age_seconds < 120
# grinder_reconcile_mismatch_total{type="ORDER_EXISTS_UNEXPECTED"} 0
# grinder_reconcile_mismatch_total{type="POSITION_NONZERO_UNEXPECTED"} 0
```

### 1.2 Review Mismatch History

```bash
# Check for recent mismatches in logs
grep "RECONCILE_MISMATCH" /var/log/grinder/app.log | tail -20

# Should be empty or show resolved issues
```

### 1.3 Verify Symbol Whitelist

Ensure symbol whitelist matches your intended trading scope:

```python
# Current whitelist
print(f"whitelist={executor.symbol_whitelist}")

# Expected: symbols you actively trade
# e.g., ["BTCUSDT", "ETHUSDT"]
```

## Stage 2: Enable Dry-Run Mode

Dry-run mode plans remediation actions but doesn't execute them.

### 2.1 Configure Dry-Run

```python
from grinder.reconcile.config import ReconcileConfig, RemediationAction

config = ReconcileConfig(
    # Passive settings (unchanged)
    enabled=True,

    # Remediation settings (dry-run first!)
    action=RemediationAction.CANCEL_ALL,  # Start with cancel only
    dry_run=True,                         # CRITICAL: Keep True
    allow_active_remediation=True,        # Allow planning
)
```

### 2.2 Deploy and Monitor

```bash
# Deploy configuration change
# (deployment method depends on your setup)

# Monitor planned actions
watch -n 10 'curl -s http://localhost:9090/metrics | grep action_planned'

# Check runner logs
tail -f /var/log/grinder/app.log | grep "REMEDIATION_PLANNED\|RECONCILE_RUN"
```

### 2.3 Review Dry-Run Plans

Run for at least 1 hour and review plans:

```bash
# Count planned actions by type
curl -s http://localhost:9090/metrics | grep action_planned

# Expected:
# grinder_reconcile_action_planned_total{action="cancel_all"} N
# grinder_reconcile_action_planned_total{action="flatten"} 0  # if using CANCEL_ALL
```

**STOP if:**
- Planned actions seem excessive (>10 per hour)
- Mismatches are expected behavior (manual orders, etc.)
- Any uncertainty about what would be cancelled

### 2.4 Enable Artifact Run-Directory (Recommended)

Enable structured artifact storage for post-mortem analysis:

```bash
# Enable artifact run-directory (M4.1)
export GRINDER_ARTIFACTS_DIR=/var/lib/grinder/artifacts
export GRINDER_ARTIFACT_TTL_DAYS=14  # Optional: days to keep old runs

# Run reconcile - artifacts are auto-created in run-dir
PYTHONPATH=src python3 -m scripts.run_live_reconcile --duration 60

# Find today's run-dirs
ls -la $GRINDER_ARTIFACTS_DIR/$(date +%Y-%m-%d)/

# View latest run's artifacts
RUN_DIR=$(ls -d $GRINDER_ARTIFACTS_DIR/$(date +%Y-%m-%d)/run_* | tail -1)
cat $RUN_DIR/stdout.log       # Run summary
tail -f $RUN_DIR/audit.jsonl | jq .  # Audit events
cat $RUN_DIR/budget_state.json       # Budget snapshot
```

Each run-dir contains:
- `stdout.log`: Config summary and exit code
- `audit.jsonl`: Audit events (RECONCILE_RUN, REMEDIATE_*)
- `metrics.prom`: Prometheus metrics snapshot
- `metrics_summary.json`: Metrics summary
- `budget_state.json`: Budget state at run end

**Backward compatibility:** Explicit `--audit-out`/`--metrics-out` flags still work
and take precedence over run-dir paths.

See ADR-046 for audit schema details.

## Stage 3: Enable Real Execution (Minimal Limits)

Only proceed after Stage 2 shows expected behavior.

### 3.1 Configure Minimal Limits

```python
config = ReconcileConfig(
    # Passive settings
    enabled=True,

    # Remediation settings (minimal limits)
    action=RemediationAction.CANCEL_ALL,
    dry_run=False,                        # NOW False
    allow_active_remediation=True,
    max_orders_per_action=1,              # Start with 1
    max_symbols_per_action=1,             # Start with 1
    cooldown_seconds=300,                 # 5 minute cooldown
)

executor = RemediationExecutor(
    config=config,
    port=futures_port,
    armed=True,                           # NOW True
    symbol_whitelist=["BTCUSDT"],         # Single symbol first
)
```

### 3.2 Set Environment Variable

```bash
# This is a safety gate - must be explicitly set
export ALLOW_MAINNET_TRADE=1

# Verify it's set
echo $ALLOW_MAINNET_TRADE
# Should print: 1
```

### 3.3 Deploy and Monitor

```bash
# Deploy configuration change

# Monitor executed actions
watch -n 10 'curl -s http://localhost:9090/metrics | grep action_executed'

# Check runner logs for executions
tail -f /var/log/grinder/app.log | grep "REMEDIATION_EXECUTED\|RECONCILE_RUN"
```

### 3.4 Verify First Execution

Wait for first real execution and verify:

```bash
# Check execution count
curl -s http://localhost:9090/metrics | grep action_executed

# Verify order was actually cancelled on exchange
# (use Binance web interface or API)
```

## Stage 4: Gradual Limit Increase

After successful Stage 3 (24+ hours), gradually increase limits.

### 4.1 Increase Order Limits

```python
# Week 1: Single symbol, 3 orders/run
max_orders_per_action=3

# Week 2: Single symbol, 5 orders/run
max_orders_per_action=5

# Week 3: Multiple symbols
max_symbols_per_action=2

# Week 4: Production limits
max_orders_per_action=10
max_symbols_per_action=3
cooldown_seconds=60
```

### 4.2 Enable Flatten (Optional)

Only after cancel is stable:

```python
# Switch to flatten mode
action=RemediationAction.FLATTEN

# Start with small notional limit
max_flatten_notional_usdt=Decimal("100")  # $100 max

# Gradually increase
max_flatten_notional_usdt=Decimal("500")  # $500 max (default)
```

## Metrics Reference

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `grinder_reconcile_runs_with_mismatch_total` | Counter | Runs with mismatches | >10/hour |
| `grinder_reconcile_runs_with_remediation_total{action}` | Counter | Runs with executed actions | >5/hour |
| `grinder_reconcile_last_remediation_ts_ms` | Gauge | Last remediation timestamp | >24h ago |
| `grinder_reconcile_action_planned_total{action}` | Counter | Dry-run plans | >10/hour |
| `grinder_reconcile_action_executed_total{action}` | Counter | Real executions | >5/hour |
| `grinder_reconcile_action_blocked_total{reason}` | Counter | Blocked actions | varies |

## Rollback Procedure

If issues occur at any stage:

### Immediate Rollback (30 seconds)

```python
# Option 1: Disable action
config.action = RemediationAction.NONE

# Option 2: Enable dry-run
config.dry_run = True

# Option 3: Disarm executor
executor.armed = False
```

### Environment Rollback

```bash
# Unset env var
unset ALLOW_MAINNET_TRADE
```

### Verify Rollback

```bash
# Should show 0 new executions after rollback
watch -n 10 'curl -s http://localhost:9090/metrics | grep action_executed'
```

## Troubleshooting

### Too Many Mismatches

1. Check if mismatches are expected (manual orders, etc.)
2. Review symbol whitelist
3. Check for network issues causing stale data

### Remediation Not Executing

1. Check which gate is blocking:
   ```bash
   curl -s http://localhost:9090/metrics | grep action_blocked
   ```
2. Verify all 9 gates pass (see Runbook 12)

### Unexpected Orders Being Cancelled

1. Verify grinder_ prefix protection is working
2. Check if orders are from another grinder instance
3. Consider tightening symbol whitelist

## Budget State Management (M4.2)

### First Run vs Multi-Run

**First run (clean slate):** Use `--reset-budget-state` to start with fresh budget counters:

```bash
BUDGET_STATE_PATH=/var/lib/grinder/budget.json \
GRINDER_ARTIFACTS_DIR=/var/lib/grinder/artifacts \
PYTHONPATH=src python3 -m scripts.run_live_reconcile \
  --reset-budget-state \
  --duration 60
```

Output includes: `budget_state_reset=1 path=/var/lib/grinder/budget.json`

**Multi-run (preserve budget):** Omit `--reset-budget-state` to accumulate budget usage:

```bash
BUDGET_STATE_PATH=/var/lib/grinder/budget.json \
GRINDER_ARTIFACTS_DIR=/var/lib/grinder/artifacts \
PYTHONPATH=src python3 -m scripts.run_live_reconcile \
  --duration 60
```

This ensures you hit real `BUDGET_EXHAUSTED` scenarios across runs.

### Stale Budget Warning

If the budget state file is older than 24 hours, you'll see:

```
WARNING: Budget state is stale (25.3h old, threshold=24h)
         Last modified: 2024-01-14 10:00:00 UTC
         Path: /var/lib/grinder/budget.json
         Consider using --reset-budget-state for a clean start.
```

This helps catch "forgot to reset" or "stale from yesterday" situations.

Configure threshold via `BUDGET_STATE_STALE_HOURS` env var (default: 24).

## Stage E: Non-BTC Symbol Micro-Test (Flatten)

BTCUSDT has a high minimum notional requirement, making micro-position tests difficult.
For safer initial flatten testing, choose a symbol with lower min-notional.

### E.1 Choose Low-Min-Notional Symbol (Procedure)

**Do not hardcode min-notional values** — Binance updates filters periodically.
Always verify live before testing.

#### Step 1: Query exchangeInfo

```bash
# Fetch all symbol filters
curl -s "https://fapi.binance.com/fapi/v1/exchangeInfo" | jq '.symbols[] | select(.symbol == "DOGEUSDT") | .filters'

# Or list all symbols with their MIN_NOTIONAL/NOTIONAL filter
curl -s "https://fapi.binance.com/fapi/v1/exchangeInfo" | jq -r '
  .symbols[] |
  select(.status == "TRADING") |
  (.filters[] | select(.filterType == "MIN_NOTIONAL" or .filterType == "NOTIONAL")) as $f |
  "\(.symbol): \($f.filterType) = \($f.notional // $f.minNotional)"
' | sort | head -50
```

#### Step 2: Identify Relevant Filters

Look for these filters in the response:

| Filter | Field | What it means |
|--------|-------|---------------|
| `MIN_NOTIONAL` or `NOTIONAL` | `notional` / `minNotional` | Minimum order value in USDT |
| `LOT_SIZE` | `minQty`, `stepSize` | Minimum quantity and step |
| `MARKET_LOT_SIZE` | `minQty`, `stepSize` | Market order quantity constraints |

**Note:** Binance may use `MIN_NOTIONAL` or `NOTIONAL` filter type — check both.

#### Step 3: Estimate Micro-Test Feasibility

```bash
# Example: Check DOGEUSDT current price
curl -s "https://fapi.binance.com/fapi/v1/ticker/price?symbol=DOGEUSDT" | jq '.price'

# Calculate: minNotional / price = minimum qty
# If minNotional=5 and price=0.10, then minQty ≈ 50 DOGE
```

**Goal:** Find a symbol where `minNotional` allows a position small enough for
your test budget (e.g., target notional slightly above min).

### E.2 Example Candidate Symbols

Common symbols with historically lower min-notional (as of writing):

- `DOGEUSDT`
- `MATICUSDT` / `POLUSDT`
- `1000PEPEUSDT`
- `1000SHIBUSDT`

**⚠️ VERIFY LIVE via exchangeInfo before use — filters change without notice.**

### E.3 Stage E Micro-Test Template

Once you've identified a suitable symbol:

```bash
# 1. Set strict symbol allowlist (SINGLE symbol only)
export REMEDIATION_SYMBOL_ALLOWLIST="DOGEUSDT"

# 2. Set EXTREMELY tight budgets
#    Values should be slightly above verified min-notional
export MAX_CALLS_PER_DAY=1
export MAX_CALLS_PER_RUN=1
export MAX_NOTIONAL_PER_DAY=10       # Adjust based on verified min-notional
export MAX_NOTIONAL_PER_RUN=10       # Same as above
export FLATTEN_MAX_NOTIONAL_PER_CALL=10

# 3. Enable mainnet trade (REQUIRED for real execution)
export ALLOW_MAINNET_TRADE=1

# 4. Set remediation mode to flatten
export REMEDIATION_MODE=execute_flatten

# 5. Budget state + artifacts
export BUDGET_STATE_PATH=/var/lib/grinder/budget.json
export GRINDER_ARTIFACTS_DIR=/var/lib/grinder/artifacts

# 6. Run with fresh budget
PYTHONPATH=src python3 -m scripts.run_live_reconcile \
  --reset-budget-state \
  --duration 60
```

**⚠️ CRITICAL REMINDERS:**

- Flatten uses **market orders** — higher slippage risk than limit orders
- After test: `unset ALLOW_MAINNET_TRADE` immediately
- Review artifacts before any subsequent runs
- Single symbol + single call = maximum isolation

### E.4 Verify Test Artifacts

After the micro-test run:

```bash
# Find latest run-dir
RUN_DIR=$(ls -dt $GRINDER_ARTIFACTS_DIR/$(date +%Y-%m-%d)/run_* 2>/dev/null | head -1)

# Check summary
cat $RUN_DIR/stdout.log

# Review audit trail for REMEDIATE_* events
grep "REMEDIATE" $RUN_DIR/audit.jsonl | jq .

# Check budget state
cat $RUN_DIR/budget_state.json

# Verify on exchange (Binance web/API) that position was flattened
```

### E.5 Post-Test Cleanup

```bash
# IMMEDIATELY disable mainnet trading
unset ALLOW_MAINNET_TRADE

# Reset to safe mode
export REMEDIATION_MODE=detect_only

# Verify
echo "ALLOW_MAINNET_TRADE=${ALLOW_MAINNET_TRADE:-unset}"
echo "REMEDIATION_MODE=$REMEDIATION_MODE"
```

## Checklist Summary

### Pre-Enablement
- [ ] Passive reconciliation healthy for 24+ hours
- [ ] Symbol whitelist configured
- [ ] Team notified

### Stage 2 (Dry-Run)
- [ ] dry_run=True
- [ ] Run for 1+ hour
- [ ] Review planned actions
- [ ] Plans match expectations

### Stage 3 (Real Execution)
- [ ] dry_run=False
- [ ] armed=True
- [ ] ALLOW_MAINNET_TRADE=1 set
- [ ] Minimal limits (1 order, 1 symbol)
- [ ] 5 minute cooldown
- [ ] Run for 24+ hours
- [ ] First execution verified

### Stage 4 (Production)
- [ ] Gradual limit increase
- [ ] Alerting configured
- [ ] Rollback procedure tested

### Stage E (Non-BTC Micro-Test)
- [ ] Verified min-notional via live exchangeInfo
- [ ] Single symbol in REMEDIATION_SYMBOL_ALLOWLIST
- [ ] Tight budgets (MAX_CALLS=1, notional slightly above min)
- [ ] ALLOW_MAINNET_TRADE=1 set
- [ ] --reset-budget-state used
- [ ] Artifacts reviewed post-run
- [ ] ALLOW_MAINNET_TRADE unset after test

## See Also

- [ADR-044](../DECISIONS.md#adr-044--remediation-wiring--routing-policy-lc-11) — Design decisions
- [ADR-046](../DECISIONS.md#adr-046--audit-jsonl-for-reconcileremediation-lc-11b) — Audit trail design
- [12_ACTIVE_REMEDIATION](12_ACTIVE_REMEDIATION.md) — RemediationExecutor details
- [11_RECONCILIATION_TRIAGE](11_RECONCILIATION_TRIAGE.md) — Passive reconciliation triage
