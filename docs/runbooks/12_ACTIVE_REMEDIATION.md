# Runbook 12: Active Remediation

Operational procedures for active remediation (LC-10).

## Overview

Active remediation extends passive reconciliation by taking action on detected mismatches:
- **cancel_all**: Cancel unexpected orders with grinder_ prefix
- **flatten**: Close unexpected positions with reduceOnly market orders

## Safety Architecture

**9 gates must ALL pass for real execution:**

| Gate | Check | Default | Override |
|------|-------|---------|----------|
| 1 | action != NONE | `NONE` | Set `action=cancel_all` or `action=flatten` |
| 2 | dry_run == False | `True` | Set `dry_run=False` |
| 3 | allow_active_remediation | `False` | Set `allow_active_remediation=True` |
| 4 | armed == True | `False` | Pass `armed=True` to executor |
| 5 | ALLOW_MAINNET_TRADE=1 | not set | Set env var |
| 6 | cooldown elapsed | 60s | Adjust `cooldown_seconds` |
| 7 | symbol in whitelist | required | Configure `symbol_whitelist` |
| 8 | grinder_ prefix (cancel) | required | Cannot override (safety) |
| 9 | notional <= limit (flatten) | 500 USD | Adjust `max_flatten_notional_usdt` |

**Kill-switch semantics:** Remediation is ALLOWED under kill-switch (reduces risk exposure).

## Enabling Active Remediation

### Step 1: Verify Passive Reconciliation Works

```bash
# Check reconciliation metrics
curl -s http://localhost:9090/metrics | grep grinder_reconcile

# Expected:
# grinder_reconcile_mismatch_total{type="ORDER_EXISTS_UNEXPECTED"} 0
# grinder_reconcile_runs_total 42
# grinder_reconcile_last_snapshot_age_seconds 15.3
```

### Step 2: Configure Remediation (Dry-Run First)

```python
from grinder.reconcile.config import ReconcileConfig, RemediationAction

config = ReconcileConfig(
    # Passive settings
    enabled=True,

    # Remediation settings (dry-run first!)
    action=RemediationAction.CANCEL_ALL,  # or FLATTEN
    dry_run=True,                         # Keep True until verified
    allow_active_remediation=True,
)
```

### Step 3: Monitor Dry-Run Plans

```bash
# Check planned actions (dry-run)
curl -s http://localhost:9090/metrics | grep action_planned

# Expected (if mismatches exist):
# grinder_reconcile_action_planned_total{action="cancel_all"} 3
```

### Step 4: Enable Real Execution

Only after verifying dry-run behavior:

```python
config = ReconcileConfig(
    action=RemediationAction.CANCEL_ALL,
    dry_run=False,                      # Now False
    allow_active_remediation=True,
)

executor = RemediationExecutor(
    config=config,
    port=futures_port,
    armed=True,                         # Enable execution
    symbol_whitelist=["BTCUSDT"],       # Required
)

# Set environment variable
# export ALLOW_MAINNET_TRADE=1
```

## Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `grinder_reconcile_action_planned_total{action}` | Counter | Dry-run plans (would execute) |
| `grinder_reconcile_action_executed_total{action}` | Counter | Real executions |
| `grinder_reconcile_action_blocked_total{reason}` | Counter | Blocked by safety gate |

### Block Reasons

| Reason | Meaning | Resolution |
|--------|---------|------------|
| `action_is_none` | action=NONE in config | Set action to cancel_all or flatten |
| `dry_run` | dry_run=True | Set dry_run=False when ready |
| `allow_active_remediation_false` | allow_active_remediation=False | Set to True |
| `not_armed` | armed=False | Set armed=True |
| `env_var_missing` | ALLOW_MAINNET_TRADE not set | Export env var |
| `cooldown_not_elapsed` | Recent action within cooldown | Wait or reduce cooldown_seconds |
| `symbol_not_in_whitelist` | Symbol not whitelisted | Add to symbol_whitelist |
| `no_grinder_prefix` | Order without grinder_ prefix | Manual order, cannot cancel |
| `notional_exceeds_limit` | Position notional > limit | Reduce position or increase max_flatten_notional_usdt |
| `max_orders_reached` | Hit max_orders_per_action | Wait for next run |
| `max_symbols_reached` | Hit max_symbols_per_action | Wait for next run |
| `whitelist_required` | Empty whitelist | Configure symbol_whitelist |
| `port_error` | Exchange API error | Check connectivity, credentials |
| `max_calls_per_run` | Hit per-run call limit | Wait for next run |
| `max_notional_per_run` | Cumulative notional exceeds per-run cap | Wait for next run |
| `max_calls_per_day` | Hit per-day call limit | Wait for next UTC day |
| `max_notional_per_day` | Cumulative notional exceeds per-day cap | Wait for next UTC day |

## Troubleshooting

### Remediation Not Executing

1. Check which gate is blocking:
   ```bash
   curl -s http://localhost:9090/metrics | grep action_blocked
   # Shows count by reason
   ```

2. Verify all gates pass:
   ```python
   can_exec, reason = executor.can_execute(
       symbol="BTCUSDT",
       is_cancel=True,
       client_order_id="grinder_BTCUSDT_1_123_1",
   )
   print(f"can_execute={can_exec}, reason={reason}")
   ```

### Unexpected Orders Not Being Cancelled

1. Verify order has grinder_ prefix:
   - Orders without `grinder_` prefix are protected (manual orders)
   - This is intentional safety behavior

2. Check symbol is whitelisted:
   ```python
   print(f"whitelist={executor.symbol_whitelist}")
   ```

### Position Not Being Flattened

1. Check notional limit:
   ```python
   position_amt = Decimal("1.0")
   price = Decimal("42500")
   notional = abs(position_amt) * price  # = $42,500

   if notional > config.max_flatten_notional_usdt:
       print(f"Notional {notional} exceeds limit {config.max_flatten_notional_usdt}")
   ```

2. Consider increasing limit or manual intervention for large positions

## Emergency Procedures

### Disable Remediation Immediately

```python
# Option 1: Set action to NONE
config.action = RemediationAction.NONE

# Option 2: Enable dry-run
config.dry_run = True

# Option 3: Disarm executor
executor.armed = False

# Option 4: Unset env var
# unset ALLOW_MAINNET_TRADE
```

### Manual Position Cleanup

If automatic flatten is blocked by notional limit:

```bash
# Use Binance API directly or web interface
# Close position manually
# Then verify: position should be 0
```

## Verification Checklist

Before enabling active remediation in production:

- [ ] Passive reconciliation running and healthy
- [ ] Dry-run mode shows expected behavior
- [ ] Symbol whitelist configured correctly
- [ ] Notional limits appropriate for position sizes
- [ ] Alerting configured for blocked actions
- [ ] Team aware of active remediation go-live

## Fire drill verification

A deterministic, CI-safe fire drill proves that budget enforcement gates
block execution correctly and metrics reflect the block reasons:

```bash
bash scripts/fire_drill_reconcile_budget_limits.sh
```

| Drill | What it proves | Key assertions |
|-------|---------------|----------------|
| A: Per-run notional cap | BudgetTracker blocks when cumulative notional exceeds per-run limit, metrics record `max_notional_per_run` block reason | 9 checks |
| B: Per-day notional cap | Per-day cap persists across run resets, blocks cumulative spend, UTC date key proven, smaller amounts still allowed within remaining budget | 12 checks |

This verifies **budget gate behavior, block reason mapping, and metrics rendering**,
not exchange connectivity or remediation execution. If the drill passes but budget
limits misfire in prod, check config values vs actual notional sizes.

### Artifact inventory

```
.artifacts/budget_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_metrics.txt      # Full Prometheus text after per-run cap block
  drill_a_log.txt          # Captured stderr (budget checks, block decisions)
  drill_a_state.json       # BudgetTracker state file (persistence proof)
  drill_b_metrics.txt      # Full Prometheus text after per-day cap block
  drill_b_log.txt          # Captured stderr (cross-run blocking, day key)
  drill_b_state.json       # BudgetTracker state file (cross-run persistence)
  summary.txt              # Copy/paste evidence block with exact metric lines
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

See also: [Ops Quickstart](00_OPS_QUICKSTART.md) | [Evidence Index](00_EVIDENCE_INDEX.md)

---

## See Also

- [ADR-043](../DECISIONS.md#adr-043--active-remediation-v01-lc-10) — Design decisions
- [11_RECONCILIATION_TRIAGE](11_RECONCILIATION_TRIAGE.md) — Passive reconciliation triage
- [04_KILL_SWITCH](04_KILL_SWITCH.md) — Kill-switch behavior
