# Runbook 11: Reconciliation Triage

This runbook covers procedures for investigating and responding to reconciliation mismatches detected by the passive reconciliation system (LC-09b, v0.1).

## Overview

Reconciliation v0.1 is **passive only**: it logs mismatches and updates metrics but takes no automatic actions. Operators must manually investigate and remediate.

**Mismatch Types:**
| Type | Meaning | Severity |
|------|---------|----------|
| `ORDER_MISSING_ON_EXCHANGE` | Expected order not found on exchange after grace period | HIGH |
| `ORDER_EXISTS_UNEXPECTED` | Order on exchange (grinder_ prefix) not in expected state | HIGH |
| `ORDER_STATUS_DIVERGENCE` | Expected vs observed status differs | MEDIUM |
| `POSITION_NONZERO_UNEXPECTED` | Position != 0 when expected = 0 | HIGH |

## Metrics to Monitor

```promql
# Mismatch counters by type
grinder_reconcile_mismatch_total{type="ORDER_MISSING_ON_EXCHANGE"}
grinder_reconcile_mismatch_total{type="ORDER_EXISTS_UNEXPECTED"}
grinder_reconcile_mismatch_total{type="ORDER_STATUS_DIVERGENCE"}
grinder_reconcile_mismatch_total{type="POSITION_NONZERO_UNEXPECTED"}

# REST snapshot staleness (should be < 120s normally)
grinder_reconcile_last_snapshot_age_seconds

# Reconcile run counter (should increase every minute)
grinder_reconcile_runs_total
```

## Alerting Recommendations

```yaml
# Prometheus alerting rules (add to monitoring/alerts/)
groups:
  - name: reconcile
    rules:
      - alert: ReconcileMismatchDetected
        expr: increase(grinder_reconcile_mismatch_total[5m]) > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Reconciliation mismatch detected"
          description: "Mismatch type: {{ $labels.type }}"

      - alert: ReconcileSnapshotStale
        expr: grinder_reconcile_last_snapshot_age_seconds > 300
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "REST snapshot is stale"
          description: "Last snapshot was {{ $value }}s ago"
```

## Triage Procedures

### ORDER_MISSING_ON_EXCHANGE

**Symptom:** Expected order not found in REST snapshot after grace period (5s default).

**Possible Causes:**
1. Order was rejected by exchange (insufficient margin, invalid params)
2. Order was filled before REST snapshot
3. Network issue caused placement failure
4. Exchange API outage

**Investigation Steps:**
1. Check application logs for order placement errors:
   ```bash
   grep "RECONCILE_MISMATCH" /var/log/grinder/*.log | grep "ORDER_MISSING"
   ```

2. Check Binance order history in web UI or via API:
   ```bash
   # Using Binance CLI or API
   curl "https://fapi.binance.com/fapi/v1/allOrders?symbol=BTCUSDT&timestamp=..." \
     -H "X-MBX-APIKEY: $BINANCE_API_KEY"
   ```

3. Check if order was filled (would appear in trade history)

**v0.1 Action:** Manual investigation only. No automatic cancel needed since order doesn't exist.

### ORDER_EXISTS_UNEXPECTED

**Symptom:** Order found on exchange with grinder_ prefix but not in expected state.

**Possible Causes:**
1. Orphaned order from previous session/crash
2. Expected state lost due to restart
3. Bug in expected state tracking

**Investigation Steps:**
1. Check logs for the order's client_order_id:
   ```bash
   grep "<client_order_id>" /var/log/grinder/*.log
   ```

2. Check Binance open orders:
   ```bash
   curl "https://fapi.binance.com/fapi/v1/openOrders?symbol=BTCUSDT&timestamp=..." \
     -H "X-MBX-APIKEY: $BINANCE_API_KEY"
   ```

3. Verify order was placed by this instance (check client_order_id format)

**v0.1 Action:** Manual cancel if order is truly orphaned:
```bash
# Cancel via Binance API
curl -X DELETE "https://fapi.binance.com/fapi/v1/order?symbol=BTCUSDT&origClientOrderId=<cid>&timestamp=..." \
  -H "X-MBX-APIKEY: $BINANCE_API_KEY"
```

### ORDER_STATUS_DIVERGENCE

**Symptom:** Expected status differs from observed status.

**Possible Causes:**
1. Stream event missed (WebSocket reconnect)
2. Race condition between stream and REST
3. Exchange state changed faster than expected

**Investigation Steps:**
1. Check logs for status transitions:
   ```bash
   grep "<client_order_id>" /var/log/grinder/*.log | grep -E "ORDER_TRADE_UPDATE|status"
   ```

2. Compare expected vs observed in mismatch log entry

3. Check if status eventually converged (transient divergence)

**v0.1 Action:** Usually self-corrects. If persistent, investigate stream health.

### POSITION_NONZERO_UNEXPECTED

**Symptom:** Position has non-zero amount when expected position is 0.

**Possible Causes:**
1. Fills not properly tracked
2. Position opened by external tool
3. Expected state reset/lost

**Investigation Steps:**
1. Check position risk via API:
   ```bash
   curl "https://fapi.binance.com/fapi/v2/positionRisk?symbol=BTCUSDT&timestamp=..." \
     -H "X-MBX-APIKEY: $BINANCE_API_KEY"
   ```

2. Check trade history for recent fills:
   ```bash
   curl "https://fapi.binance.com/fapi/v1/userTrades?symbol=BTCUSDT&timestamp=..." \
     -H "X-MBX-APIKEY: $BINANCE_API_KEY"
   ```

3. Verify position wasn't opened by another bot/UI

**v0.1 Action:** Manual flatten if position is truly unexpected:
```bash
# Close position via market order (requires API credentials)
# Use smoke_futures_mainnet.py with appropriate flags
```

## Escalation

If mismatches persist after investigation:

1. **Check stream health:** WebSocket connection, listenKey validity
2. **Check REST health:** API rate limits, authentication
3. **Check expected state:** Is ExpectedStateStore being updated correctly?
4. **Check time sync:** Clock drift can cause false positives

## Recovery Procedures

### Full State Reset (v0.1 manual)

If expected state is corrupted:

1. Stop grinder instance
2. Clear expected state (restart will reset)
3. Cancel all open grinder_ orders on exchange
4. Close any open positions
5. Restart grinder instance

### Stream Reconnect

If user-data stream is unhealthy:

1. Check listenKey validity (30m expiry)
2. Force reconnect by restarting connector
3. Monitor for missed events

## Future Improvements (LC-10)

v1.0 will add automatic remediation:
- `RECONCILE_ACTION=cancel_all`: Auto-cancel unexpected orders
- `RECONCILE_ACTION=flatten`: Auto-flatten unexpected positions
- Configurable action thresholds
- HA-aware reconcile loop (only leader reconciles)

## Related

- [ADR-042](../DECISIONS.md): Passive Reconciliation v0.1 design
- [ADR-041](../DECISIONS.md): Futures User-Data Stream v0.1
- [STATE.md](../STATE.md): Current implementation status
