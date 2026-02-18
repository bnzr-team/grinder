# Runbook 26: Fill Tracker Triage (Launch-06)

## Overview

The FillTracker records fill events and emits Prometheus counters:
- `grinder_fills_total{source,side,liquidity}` — fill count
- `grinder_fill_notional_total{source,side,liquidity}` — cumulative notional
- `grinder_fill_fees_total{source,side,liquidity}` — cumulative fees

**Labels**: `source` (reconcile/sim/manual/none), `side` (buy/sell/none), `liquidity` (maker/taker/none).

**Current state (Launch-06 PR1)**: detect-only scaffold. Metrics are wired into `/metrics` but no live execution paths call `FillTracker.record()` yet. Counters will show placeholder zeros until future PRs connect real fill sources.

---

## What "good" looks like

### Before wiring (PR1)

Placeholder counters at zero:

```
grinder_fills_total{source="none",side="none",liquidity="none"} 0
grinder_fill_notional_total{source="none",side="none",liquidity="none"} 0
grinder_fill_fees_total{source="none",side="none",liquidity="none"} 0
```

### After wiring (future PRs)

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

**Expected** until live fill sources are wired (future PRs). No action needed.

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
