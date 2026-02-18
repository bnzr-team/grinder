# Runbook 23: Data Quality Triage

Operational guide for data quality (DQ) alerts and safe rollback (Launch-04).

## Overview

This runbook covers:
- DQ-specific Prometheus alerts (staleness, gaps, outliers, gate blocks)
- How to distinguish "exchange lagging" vs "our ingestion degraded"
- Safe rollback of DQ flags (`dq_enabled`, `dq_blocking`)
- Evidence bundle for post-incident review

**Prerequisites:** DQ metrics and gating are wired via Launch-03 (PR #176-#178).

---

## Alerts Reference

| Alert | Severity | Meaning |
|-------|----------|---------|
| GrinderDataStaleBurst | page | Staleness events firing rapidly -- exchange or ingestion lagging |
| GrinderDataGapSpike | ticket | Tick gaps elevated -- possible connectivity issue |
| GrinderDataOutlierSpike | ticket | Price outlier events elevated -- flash crash or glitch |
| GrinderDQBlockingActive | page | DQ gate is actively blocking remediation actions |

All alerts use `stream=` labels only. No `symbol=` labels anywhere.

---

## Quick Diagnosis

### Step 1: Which alert fired?

| Alert | Go to |
|-------|-------|
| GrinderDataStaleBurst | [Staleness triage](#staleness-triage) |
| GrinderDataGapSpike | [Gap triage](#gap-triage) |
| GrinderDataOutlierSpike | [Outlier triage](#outlier-triage) |
| GrinderDQBlockingActive | [Gate blocking triage](#gate-blocking-triage) |

### Step 2: Check current DQ metrics

```bash
# All DQ counters at a glance
curl -sf http://localhost:9090/metrics | grep -E 'grinder_data_(stale|gap|outlier)_total'

# DQ-related gate blocks
curl -sf http://localhost:9090/metrics | grep 'grinder_reconcile_action_blocked_total' | grep 'data_quality'

# General health
curl -sf http://localhost:9090/healthz | python3 -m json.tool
```

---

## Staleness Triage

**Alert:** `GrinderDataStaleBurst`

**What it means:** The live feed is receiving ticks with timestamps that look stale relative to wall-clock time. This is the strongest signal that data is unreliable.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Exchange WebSocket lagging | Check exchange status page; compare with other feeds |
| Network latency spike | `docker logs grinder 2>&1 \| grep -i 'timeout\|disconnect\|reconnect'` |
| Container clock drift | `docker exec grinder date -u` vs `date -u` on host |
| Ingestion pipeline stalled | `curl -sf .../metrics \| grep grinder_uptime_seconds` (is it still incrementing?) |

**Actions:**
1. If exchange is down: wait for recovery, staleness will self-resolve.
2. If network issue: check connectivity, WebSocket reconnection logs.
3. If clock drift: restart container (`docker compose restart grinder`).
4. If pipeline stalled: check logs, consider restart.

---

## Gap Triage

**Alert:** `GrinderDataGapSpike`

**What it means:** Gaps between consecutive tick timestamps exceed configured thresholds. May indicate dropped ticks or exchange maintenance.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Exchange maintenance | Check exchange announcements |
| WebSocket disconnection | `docker logs grinder 2>&1 \| grep -i 'disconnect\|reconnect\|closed'` |
| High system load | `docker stats grinder` (CPU/memory) |
| Rate limiting | `docker logs grinder 2>&1 \| grep -i 'rate.limit\|429'` |

**Actions:**
1. Short gaps during exchange maintenance are expected -- monitor and wait.
2. Persistent gaps: check WebSocket health, consider reconnection.
3. If load-related: investigate resource constraints.

---

## Outlier Triage

**Alert:** `GrinderDataOutlierSpike`

**What it means:** Price movements exceed the configured BPS threshold (default: 500 bps). Could be a real market event or a data glitch.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Flash crash / real volatility | Check price on exchange UI, cross-reference other sources |
| Exchange data glitch | Single erratic tick followed by normal prices |
| Misconfigured threshold | Too tight for the asset's normal volatility |

**Actions:**
1. If real market event: outlier filter is working correctly, no action needed.
2. If data glitch: the DQ gate (if `dq_blocking=True`) will prevent trading on bad data.
3. If threshold too tight: adjust `outlier_bps` in `DataQualityConfig` and restart.

---

## Gate Blocking Triage

**Alert:** `GrinderDQBlockingActive`

**What it means:** The DQ gate (Gate 0e in remediation) is actively blocking remediation actions because `dq_blocking=True` and the latest DQ verdict indicates a problem.

This is the **most operationally important** alert -- it proves that bad data quality is directly preventing the system from taking remediation actions.

**Check which reason is blocking:**

```bash
curl -sf http://localhost:9090/metrics \
  | grep 'grinder_reconcile_action_blocked_total' \
  | grep 'data_quality'
```

Expected output (one or more of):
```
grinder_reconcile_action_blocked_total{reason="data_quality_stale"} N
grinder_reconcile_action_blocked_total{reason="data_quality_gap"} N
grinder_reconcile_action_blocked_total{reason="data_quality_outlier"} N
```

**Actions:**
1. Identify the root cause using the triage sections above.
2. If DQ is blocking and you need remediation to proceed immediately, see [Safe Rollback](#safe-rollback).
3. If DQ is correctly blocking bad data, no action needed -- the system is protecting itself.

---

## Safe Rollback

### Disable DQ blocking only (keep metrics collection)

This allows remediation to proceed while still collecting DQ metrics for observability.

**How:** Set `dq_blocking=False` in `LiveFeedConfig` and restart.

```bash
# dq_blocking is a Python config flag on LiveFeedConfig (Source: feed.py:61)
# and on RemediationExecutor (Source: remediation.py:172).
# Set to False in your launch config / constructor and restart:
docker compose restart grinder
```

**Verify rollback:**
```bash
# DQ gate blocks should stop incrementing
curl -sf http://localhost:9090/metrics \
  | grep 'grinder_reconcile_action_blocked_total' \
  | grep 'data_quality'
# Values should stop increasing (watch over 60s)

# DQ metrics should still be collected
curl -sf http://localhost:9090/metrics \
  | grep -E 'grinder_data_(stale|gap|outlier)_total'
# Counters should still increment (dq_enabled is still True)
```

### Disable DQ entirely (metrics + blocking)

Use this only if DQ detection itself is causing overhead or noise.

**How:** Set `dq_enabled=False` in `LiveFeedConfig` and restart.

```bash
# dq_enabled is a Python config flag on LiveFeedConfig (Source: feed.py:59).
# When False, DataQualityEngine is not imported or instantiated.
# Set to False in your launch config / constructor and restart:
docker compose restart grinder
```

**Verify rollback:**
```bash
# DQ metrics should stop incrementing entirely
curl -sf http://localhost:9090/metrics \
  | grep -E 'grinder_data_(stale|gap|outlier)_total'
# Counters should be zero or absent

# Health should still be OK
curl -sf http://localhost:9090/healthz | python3 -m json.tool
```

### Re-enable DQ after issue is resolved

1. Set `dq_enabled=True` (metrics collection).
2. Monitor DQ metrics for 10-15 minutes to confirm data quality is acceptable.
3. Set `dq_blocking=True` (gate enforcement).
4. Restart: `docker compose restart grinder`.
5. Verify: `GrinderDQBlockingActive` alert does not fire.

---

## Decision Tree

```
Alert fired
  |
  +-- GrinderDataStaleBurst or GrinderDataGapSpike
  |     |
  |     +-- Is exchange having issues?
  |     |     YES -> Wait for recovery. DQ gate will auto-unblock
  |     |            when data quality improves.
  |     |     NO  -> Check network, logs, container health.
  |     |
  |     +-- Is remediation needed urgently?
  |           YES -> Rollback: dq_blocking=False + restart
  |           NO  -> Monitor. Gate is protecting correctly.
  |
  +-- GrinderDataOutlierSpike
  |     |
  |     +-- Real market event?
  |     |     YES -> No action. Filter is working correctly.
  |     |     NO  -> Data glitch. Gate blocks if dq_blocking=True.
  |     |            Consider adjusting outlier_bps threshold.
  |
  +-- GrinderDQBlockingActive
        |
        +-- Check blocking reason (stale/gap/outlier)
        +-- Triage root cause using sections above
        +-- If false positive: dq_blocking=False + restart
        +-- If correct: system is self-protecting, monitor
```

---

## Evidence Bundle

When escalating or performing post-incident review, save:

| Artifact | Command |
|----------|---------|
| Metrics snapshot | `curl -sf http://localhost:9090/metrics > metrics_$(date +%s).txt` |
| DQ-specific metrics | `curl -sf http://localhost:9090/metrics \| grep -E 'grinder_data_\|data_quality' > dq_metrics_$(date +%s).txt` |
| Container logs | `docker logs grinder --since 30m > grinder_logs_$(date +%s).txt 2>&1` |
| Health endpoint | `curl -sf http://localhost:9090/healthz > healthz_$(date +%s).json` |
| Git SHA | `git -C /path/to/grinder rev-parse HEAD` |
| Timestamp | `date -u` |
| Audit trail | `cp audit/reconcile.jsonl audit_backup_$(date +%s).jsonl` (if `GRINDER_AUDIT_ENABLED=1`) |

---

## Follow-up

After resolving a DQ incident:

1. Review whether thresholds need tuning based on observed false-positive rate.
2. Consider adjusting alert thresholds in `monitoring/alert_rules.yml` (marked "tune after 1 week").
3. Update `DataQualityConfig` thresholds if outlier/gap/staleness defaults are too tight.
4. Document the incident and resolution in the team's incident log.
