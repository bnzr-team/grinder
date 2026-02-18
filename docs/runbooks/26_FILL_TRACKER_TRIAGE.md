# Runbook 26: Fill Tracker Triage (Launch-06)

## Overview

The FillTracker records fill events and emits Prometheus counters:
- `grinder_fills_total{source,side,liquidity}` — fill count
- `grinder_fill_notional_total{source,side,liquidity}` — cumulative notional
- `grinder_fill_fees_total{source,side,liquidity}` — cumulative fees

**Labels**: `source` (reconcile/sim/manual/none), `side` (buy/sell/none), `liquidity` (maker/taker/none).

**Current state (Launch-06 PR2)**: FillTracker is wired into the reconcile loop. When `FILL_INGEST_ENABLED=1`, each reconcile iteration fetches `userTrades` from Binance, ingests them into FillTracker, and pushes counters to FillMetrics. A persistent cursor file (`FILL_CURSOR_PATH`) prevents re-reading trades after restarts.

---

## What "good" looks like

### Feature OFF (FILL_INGEST_ENABLED unset or "0")

Placeholder counters at zero:

```
grinder_fills_total{source="none",side="none",liquidity="none"} 0
grinder_fill_notional_total{source="none",side="none",liquidity="none"} 0
grinder_fill_fees_total{source="none",side="none",liquidity="none"} 0
```

### Feature ON (FILL_INGEST_ENABLED=1) with trades

Counters increment with real labels:

```
grinder_fills_total{source="reconcile",side="buy",liquidity="taker"} 42
grinder_fill_notional_total{source="reconcile",side="buy",liquidity="taker"} 12345.67
grinder_fill_fees_total{source="reconcile",side="buy",liquidity="taker"} 4.93
```

---

## Triage decision tree

### Metrics missing entirely

```
curl -sf http://localhost:9090/metrics | grep grinder_fill
```

If no output:
1. Check that grinder container is running: `docker-compose ps`
2. Check `/healthz`: `curl -sf http://localhost:9090/healthz`
3. Check that Launch-06 PR1 is deployed (code includes `fill_metrics.py`)

### Counters still at zero (placeholder only)

1. Check `FILL_INGEST_ENABLED` is set to `"1"` in the environment.
2. Check reconcile loop is actually running: `grinder_reconcile_runs_total` > 0.
3. If no trades on the account, counters stay at zero (correct behavior).

### Counters growing but values seem wrong

1. Check fill source: `source=` label tells you where fills come from
2. Compare `buy` vs `sell` notional — should be roughly balanced for grid strategy
3. Compare `maker` vs `taker` — high taker ratio may indicate aggressive fills
4. Check fees: `grinder_fill_fees_total` should be small relative to notional

### Sudden spike in fill count

1. Check if reconcile loop ran: `grinder_reconcile_runs_total`
2. Check for market volatility (external)
3. If fills are from `source="sim"`, this is paper trading (no real money)

---

### Cursor issues

If fills seem to repeat after restart or skip trades:

1. Check cursor file: `cat $FILL_CURSOR_PATH`
2. Verify `last_trade_id` matches expected Binance trade ID
3. To reset cursor: delete file and restart (`rm $FILL_CURSOR_PATH`)

### Enabling fill ingestion

```bash
# Add to docker-compose override or .env:
FILL_INGEST_ENABLED=1
FILL_CURSOR_PATH=/var/lib/grinder/fill_cursor.json

# Restart reconcile process
docker-compose restart grinder
```

---

## Evidence bundle commands

```bash
# 1. Current fill metrics
curl -sf http://localhost:9090/metrics | grep grinder_fill

# 2. Health check
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# 3. Fill metrics contract check
curl -sf http://localhost:9090/metrics | grep -c grinder_fill

# 4. Check for forbidden labels
curl -sf http://localhost:9090/metrics | grep grinder_fill | grep -E 'symbol=|order_id=|client_id=' || echo "CLEAN: no forbidden labels"

# 5. Recent container logs
docker logs grinder --since 5m 2>&1 | tail -30

# 6. Reconcile state (for context)
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total

# 7. Full metrics snapshot (for archival)
curl -sf http://localhost:9090/metrics > /tmp/metrics_snapshot_$(date +%s).txt
```
