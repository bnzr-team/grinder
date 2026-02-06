# Runbook 14: E2E Reconcile→Remediate Smoke Test

End-to-end smoke test for reconciliation and remediation flow (LC-13).

## Overview

This runbook guides you through running the E2E smoke test for the reconcile→remediate pipeline. The smoke test validates:

1. Order mismatch detection → CANCEL routing
2. Position mismatch detection → FLATTEN routing
3. Mixed mismatch priority routing (order wins over position)
4. Dry-run mode (zero port calls)
5. Live mode gating (5 safety gates)
6. Audit integration when enabled

## Prerequisites

- [ ] Python 3.11+ installed
- [ ] GRINDER codebase checked out
- [ ] Active remediation module deployed (LC-10, LC-11)

## Quick Start (Dry-Run)

The default mode is **DRY-RUN** — safe to run without credentials or env vars.

```bash
cd /path/to/grinder
PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e
```

Expected output:
```
============================================================
  CONFIGURATION
============================================================
  Mode:              DRY-RUN
  action:            cancel_all
  dry_run:           True
  allow_active:      False
  armed:             False
  symbol_whitelist:  ['BTCUSDT']
  ...

--- Scenario: order [PASS] ---
  Mismatches detected:  1
  Expected action:      cancel
  Actual action:        cancel
  Port calls:           0
  ...

--- Scenario: position [PASS] ---
  ...

--- Scenario: mixed [PASS] ---
  ...

============================================================
  SUMMARY
============================================================
  Mode:    DRY-RUN
  Passed:  3/3

  ALL SCENARIOS PASSED
```

## Dry-Run with Audit

Enable audit trail to verify JSONL integration:

```bash
GRINDER_AUDIT_ENABLED=1 PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e
```

Check audit file:
```bash
cat audit/reconcile.jsonl | jq .
```

## 3 P0 Scenarios

### Scenario 1: Order Mismatch → CANCEL

**Setup:**
- Inject unexpected order (grinder_ prefix)
- No expected position

**Expected behavior:**
- Engine detects `ORDER_EXISTS_UNEXPECTED` mismatch
- Runner routes to `cancel_all` action
- In dry-run: `planned_count=1`, `executed_count=0`, `port_calls=0`

### Scenario 2: Position Mismatch → FLATTEN

**Setup:**
- Inject unexpected position (qty > 0)
- Expected position qty = 0

**Expected behavior:**
- Engine detects `POSITION_NONZERO_UNEXPECTED` mismatch
- Runner routes to `flatten` action
- In dry-run: `planned_count=1`, `executed_count=0`, `port_calls=0`

### Scenario 3: Mixed Mismatches → Priority Routing

**Setup:**
- Inject both unexpected order AND unexpected position
- Action = CANCEL_ALL

**Expected behavior:**
- Engine detects both mismatches
- Order mismatch has higher priority (10-20) than position (100)
- With `action=CANCEL_ALL`: only order mismatch routes → cancel
- In dry-run: `planned_count=1` (order only)

## Live Mode (Operator Ceremony)

Live mode requires **all 5 gates** to pass:

| # | Gate | How to Pass |
|---|------|-------------|
| 1 | CLI confirm | `--confirm LIVE_REMEDIATE` |
| 2 | Dry-run disabled | `RECONCILE_DRY_RUN=0` |
| 3 | Active allowed | `RECONCILE_ALLOW_ACTIVE=1` |
| 4 | Executor armed | `ARMED=1` |
| 5 | Mainnet allowed | `ALLOW_MAINNET_TRADE=1` |

### Check Gate Status

Run without env vars to see which gates fail:

```bash
PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e --confirm LIVE_REMEDIATE
```

Output (gates not set):
```
============================================================
  LIVE MODE GATE CHECK FAILED
============================================================
  The following gates are not satisfied:
    - RECONCILE_DRY_RUN must be '0' (got '1')
    - RECONCILE_ALLOW_ACTIVE must be '1' (got '0')
    - ARMED must be '1' (got '0')
    - ALLOW_MAINNET_TRADE must be '1' (got '0')

  Live mode requires ALL gates to pass.
  Use default mode (no --confirm) for dry-run.
```

### Enable Live Mode

```bash
RECONCILE_DRY_RUN=0 \
RECONCILE_ALLOW_ACTIVE=1 \
ARMED=1 \
ALLOW_MAINNET_TRADE=1 \
PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e --confirm LIVE_REMEDIATE
```

In live mode:
- `executed_count > 0` (real execution)
- `port_calls > 0` (real port calls recorded by FakePort)
- Audit events show `mode: "live"`

## Verification Checklist

### Dry-Run Mode
- [ ] All 3 scenarios pass
- [ ] `Port calls: 0` for all scenarios
- [ ] `Executed count: 0` for all scenarios
- [ ] `Planned count: 1` for order and position scenarios

### Audit Integration
- [ ] `GRINDER_AUDIT_ENABLED=1` creates audit file
- [ ] Audit file contains RECONCILE_RUN events
- [ ] Events include mismatch counts and action

### Live Mode Gating
- [ ] Without env vars: shows gate failures
- [ ] With all env vars: executes in live mode
- [ ] `Port calls > 0` in live mode

## Troubleshooting

### "ModuleNotFoundError: No module named 'grinder'"

Ensure PYTHONPATH is set:
```bash
PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e
```

### Scenario Fails with Wrong Action

Check the config used for each scenario:
- Order scenario uses `action=CANCEL_ALL`
- Position scenario uses `action=FLATTEN`
- Mixed scenario uses `action=CANCEL_ALL` (order wins by priority)

### Audit File Not Created

Verify env var is set:
```bash
echo $GRINDER_AUDIT_ENABLED  # Should print "1"
```

### Live Mode Still Shows Gate Failures

All 5 gates must be set. Check each:
```bash
echo "DRY_RUN=$RECONCILE_DRY_RUN"
echo "ALLOW_ACTIVE=$RECONCILE_ALLOW_ACTIVE"
echo "ARMED=$ARMED"
echo "MAINNET=$ALLOW_MAINNET_TRADE"
```

## Integration with CI

Add to CI pipeline:
```yaml
- name: E2E Reconcile Smoke
  run: |
    PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e
```

With audit verification:
```yaml
- name: E2E Reconcile Smoke (with audit)
  run: |
    GRINDER_AUDIT_ENABLED=1 PYTHONPATH=src python3 -m scripts.smoke_reconcile_e2e
    # Verify audit file exists and has expected events
    test -f audit/reconcile.jsonl
    grep -q "RECONCILE_RUN" audit/reconcile.jsonl
```

## See Also

- [ADR-047](../DECISIONS.md#adr-047--e2e-reconcileremediate-smoke-harness-lc-13) — Design decisions
- [11_RECONCILIATION_TRIAGE](11_RECONCILIATION_TRIAGE.md) — Passive reconciliation triage
- [12_ACTIVE_REMEDIATION](12_ACTIVE_REMEDIATION.md) — Active remediation operations
- [13_OPERATOR_CEREMONY](13_OPERATOR_CEREMONY.md) — Staged enablement procedure
