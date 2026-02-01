# Runbook: Kill-Switch Events

## Overview

The kill-switch is a safety mechanism that halts all trading when risk limits are breached. This runbook covers detection, diagnosis, and recovery.

---

## What Triggers Kill-Switch

| Reason | Condition | Metric |
|--------|-----------|--------|
| Daily Loss Limit | Drawdown exceeds 5% | `grinder_drawdown_pct > 5` |
| Manual | Operator-initiated | N/A |

---

## Detection

### 1. Check Kill-Switch Status

```bash
curl -fsS http://localhost:9090/metrics | grep "grinder_kill_switch_triggered"
```

**Normal (trading active):**

```
grinder_kill_switch_triggered 0
```

**Kill-switch active (trading halted):**

```
grinder_kill_switch_triggered 1
```

### 2. Check Trip Reason

```bash
curl -fsS http://localhost:9090/metrics | grep "grinder_kill_switch_trips_total"
```

**Example output:**

```
grinder_kill_switch_trips_total{reason="DAILY_LOSS_LIMIT"} 1
grinder_kill_switch_trips_total{reason="MANUAL"} 0
```

### 3. Check Current Drawdown

```bash
curl -fsS http://localhost:9090/metrics | grep "grinder_drawdown_pct"
```

---

## Diagnosis

### Decision Tree

```
Is kill_switch_triggered == 1?
├── YES → Check trips_total for reason
│   ├── DAILY_LOSS_LIMIT → Drawdown exceeded 5%
│   │   └── Check grinder_drawdown_pct value
│   └── MANUAL → Operator triggered manually
└── NO → Kill-switch not active, system trading normally
```

### Get Full Risk State

```bash
curl -fsS http://localhost:9090/metrics | grep -E "grinder_kill|grinder_drawdown|grinder_high_water"
```

**Example output when triggered:**

```
grinder_kill_switch_triggered 1
grinder_kill_switch_trips_total{reason="DAILY_LOSS_LIMIT"} 1
grinder_drawdown_pct 5.23
grinder_high_water_mark 10000.00
```

---

## Recovery

### Automatic Reset

The kill-switch resets automatically at:
- Start of new trading day (UTC midnight)
- Service restart with fresh state

### Manual Reset via Restart

If you need to reset kill-switch immediately:

```bash
# Stop and restart the service
docker compose -f docker-compose.observability.yml restart grinder

# Verify reset
curl -fsS http://localhost:9090/metrics | grep "grinder_kill_switch_triggered"
```

**Expected after restart:**

```
grinder_kill_switch_triggered 0
grinder_drawdown_pct 0
```

**Warning:** Restarting resets all state including high-water mark. Only do this after understanding why the kill-switch triggered.

---

## Alert Response

When you receive a `KillSwitchTripped` alert:

1. **Acknowledge** the alert
2. **Verify** kill-switch state via metrics
3. **Investigate** the trip reason
4. **Document** the incident
5. **Decide** whether to wait for auto-reset or manual restart

### Investigation Checklist

- [ ] What was the trip reason?
- [ ] What was the drawdown at trigger time?
- [ ] Were there any unusual market conditions?
- [ ] Are there errors in logs? (`docker logs grinder --tail=100`)
- [ ] Should we restart or wait for next trading day?

---

## Prevention

- Monitor `grinder_drawdown_pct` for early warning
- Set `HighDrawdown` alert threshold below kill-switch threshold
- Review position sizing if frequent triggers occur
