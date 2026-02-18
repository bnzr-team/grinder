# Runbook 24: HTTP Latency / Retry Triage

Operational guide for HTTP latency and retry alerts (Launch-05 PR3).

## Overview

This runbook covers:
- HTTP latency/retry Prometheus alerts (deadline misses, p99 latency, retries, rate limits)
- How to distinguish "exchange degraded" vs "our config too tight"
- Safe rollback of latency/retry flags (`LATENCY_RETRY_ENABLED`)
- Evidence bundle for post-incident review

**Prerequisites:** HTTP metrics and retry wiring via Launch-05 PR1 (PR #180) and PR2 (PR #181).

**Metrics source:** `src/grinder/observability/latency_metrics.py`

**Labels:** `op=` only. No `symbol=` labels anywhere.

---

## Alerts Reference

| Alert | Severity | Meaning |
|-------|----------|---------|
| GrinderHttpWriteDeadlineMissBurst | page | Write-op timeouts elevated -- cancel/place orders may not reach exchange |
| GrinderHttpCancelLatencyP99High | page | cancel_order p99 latency exceeds 600ms deadline budget |
| GrinderHttpReadRetriesSpike | ticket | Read-op retries sustained -- connectivity degraded |
| GrinderHttp429RateLimitSpike | ticket | Exchange rate-limiting (429) -- reduce request frequency |

All thresholds are conservative. Tune after 1-2 weeks of data.

---

## Quick Diagnosis

### Step 1: Which alert fired?

| Alert | Go to |
|-------|-------|
| GrinderHttpWriteDeadlineMissBurst | [Write deadline triage](#write-deadline-triage) |
| GrinderHttpCancelLatencyP99High | [Cancel latency triage](#cancel-latency-triage) |
| GrinderHttpReadRetriesSpike | [Read retries triage](#read-retries-triage) |
| GrinderHttp429RateLimitSpike | [Rate limit triage](#rate-limit-triage) |

### Step 2: Check current HTTP metrics

```bash
# All HTTP counters at a glance
curl -sf http://localhost:9090/metrics | grep -E 'grinder_http_(requests|retries|fail)_total'

# Latency histogram summary (sum and count per op)
curl -sf http://localhost:9090/metrics | grep -E 'grinder_http_latency_ms_(sum|count)'

# General health
curl -sf http://localhost:9090/healthz | python3 -m json.tool
```

---

## Write Deadline Triage

**Alert:** `GrinderHttpWriteDeadlineMissBurst`

**What it means:** Write operations (cancel_order, place_order, cancel_all) are failing with timeout. Orders may not be reaching the exchange in time.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Exchange API latency spike | Check exchange status page; compare with other traders/feeds |
| Network degradation | `docker logs grinder 2>&1 \| grep -i 'timeout\|connect'` |
| Deadline budget too tight | Check `HTTP_DEADLINE_CANCEL_ORDER_MS` / `HTTP_DEADLINE_PLACE_ORDER_MS` values |
| DNS resolution slow | `docker logs grinder 2>&1 \| grep -i 'dns\|resolve'` |

**Actions:**

1. Check which specific ops are timing out:
   ```bash
   curl -sf http://localhost:9090/metrics \
     | grep 'grinder_http_fail_total' \
     | grep 'timeout'
   ```

2. Check current latency percentiles:
   ```bash
   # Latency histogram for write ops
   curl -sf http://localhost:9090/metrics \
     | grep -E 'grinder_http_latency_ms.*(cancel_order|place_order|cancel_all)'
   ```

3. If exchange is degraded: wait for recovery, timeouts will self-resolve.
4. If deadline is too tight: increase via env var and restart:
   ```bash
   export HTTP_DEADLINE_CANCEL_ORDER_MS=1000  # Source: run_live_reconcile.py:420
   docker compose restart grinder
   ```
5. If persistent: consider rollback (see [Safe Rollback](#safe-rollback)).

---

## Cancel Latency Triage

**Alert:** `GrinderHttpCancelLatencyP99High`

**What it means:** The 99th percentile latency for cancel_order exceeds the 600ms deadline budget. Most cancel attempts are at risk of timing out.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Exchange cancel endpoint slow | Check Binance API status; try manual cancel via API |
| High order volume increasing cancel latency | Check `grinder_http_requests_total{op="cancel_order"}` rate |
| Network latency to exchange | `ping api.binance.com` or check traceroute |
| Server-side queueing at exchange | Check if `grinder_http_latency_ms_bucket{op="cancel_order",le="400"}` is low |

**Actions:**

1. Check current p99 vs deadline:
   ```bash
   # Latency buckets for cancel_order
   curl -sf http://localhost:9090/metrics \
     | grep 'grinder_http_latency_ms_bucket{op="cancel_order"'
   ```

2. Check fail and retry rates:
   ```bash
   curl -sf http://localhost:9090/metrics \
     | grep -E 'grinder_http_(fail|retries)_total.*cancel_order'
   ```

3. If exchange is slow: this is expected during high-volatility events. Monitor.
4. If deadline is too tight for normal conditions: increase budget:
   ```bash
   export HTTP_DEADLINE_CANCEL_ORDER_MS=1000  # Source: run_live_reconcile.py:420
   docker compose restart grinder
   ```
5. If p99 is consistently high (>500ms): investigate network path.

---

## Read Retries Triage

**Alert:** `GrinderHttpReadRetriesSpike`

**What it means:** Read operations (get_positions, get_account, get_open_orders, exchange_info, ping_time) are being retried at an elevated rate. This wastes latency budget and indicates degraded connectivity.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Exchange API intermittent errors | Check for 5xx in `grinder_http_retries_total{reason="5xx"}` |
| Network flapping | `docker logs grinder 2>&1 \| grep -i 'timeout\|connect\|reset'` |
| Timeouts on read ops | Check `grinder_http_retries_total{reason="timeout"}` |
| DNS issues | Check `grinder_http_retries_total{reason="dns"}` |

**Actions:**

1. Check retry reasons:
   ```bash
   curl -sf http://localhost:9090/metrics \
     | grep 'grinder_http_retries_total'
   ```

2. Check which ops are retrying:
   ```bash
   curl -sf http://localhost:9090/metrics \
     | grep 'grinder_http_retries_total' \
     | grep -v '} 0'
   ```

3. If retries are succeeding (requests eventually complete): monitor, no immediate action.
4. If retries are exhausting: check `grinder_http_fail_total` for the same ops.
5. If read deadlines too tight: increase via env var:
   ```bash
   export HTTP_DEADLINE_GET_POSITIONS_MS=4000  # Source: run_live_reconcile.py:420
   docker compose restart grinder
   ```
6. If persistent: reduce retry attempts:
   ```bash
   export HTTP_MAX_ATTEMPTS_READ=1  # Source: run_live_reconcile.py:408
   docker compose restart grinder
   ```

---

## Rate Limit Triage

**Alert:** `GrinderHttp429RateLimitSpike`

**What it means:** The exchange is returning HTTP 429 (Too Many Requests). Grinder is hitting the Binance API rate limit.

**Probable causes:**

| Cause | How to confirm |
|-------|----------------|
| Reconcile interval too short | Check `RECONCILE_INTERVAL_MS` (Source: `reconcile_loop.py:78`) |
| Too many retries amplifying requests | Check total request rate across all ops |
| Multiple grinder instances sharing same API key | Check if other processes use the same key |
| Exchange lowered rate limits | Check Binance API announcements |

**Actions:**

1. Check 429 retry rate:
   ```bash
   curl -sf http://localhost:9090/metrics \
     | grep 'grinder_http_retries_total{' \
     | grep '429'
   ```

2. Check total request rate:
   ```bash
   curl -sf http://localhost:9090/metrics \
     | grep 'grinder_http_requests_total'
   ```

3. Increase reconcile interval to reduce request frequency:
   ```bash
   export RECONCILE_INTERVAL_MS=60000  # Source: reconcile_loop.py:78
   docker compose restart grinder
   ```

4. If retries are amplifying the problem: reduce max attempts:
   ```bash
   export HTTP_MAX_ATTEMPTS_READ=1   # Source: run_live_reconcile.py:408
   export HTTP_MAX_ATTEMPTS_WRITE=1  # Source: run_live_reconcile.py:412
   docker compose restart grinder
   ```

5. If another process shares the API key: investigate and isolate.

---

## Safe Rollback

### Disable latency/retry layer (immediate pass-through)

This reverts to pre-Launch-05 behavior: no per-op deadlines, no retries, no HTTP metrics.

```bash
unset LATENCY_RETRY_ENABLED  # Source: run_live_reconcile.py:406
docker compose restart grinder
```

**Verify rollback:**

```bash
# HTTP metrics should stop incrementing (no new observations)
curl -sf http://localhost:9090/metrics \
  | grep -E 'grinder_http_(requests|retries|fail)_total'
# Values should stop increasing (watch over 60s)

# Health should still be OK
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# Reconcile loop should still run
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total
# Counter should still increment
```

### Reduce retry scope without full rollback

If only retries are problematic but per-op deadlines are fine:

```bash
export LATENCY_RETRY_ENABLED=1
export HTTP_MAX_ATTEMPTS_READ=1   # Source: run_live_reconcile.py:408
export HTTP_MAX_ATTEMPTS_WRITE=1  # Source: run_live_reconcile.py:412
docker compose restart grinder
```

This keeps per-op deadline budgets and metrics but disables all retries.

### Re-enable after issue is resolved

1. Set `LATENCY_RETRY_ENABLED=1`.
2. Monitor HTTP metrics for 10-15 minutes in STAGING (no retries yet).
3. Gradually increase `HTTP_MAX_ATTEMPTS_READ` (e.g., 2, then 3).
4. Restart: `docker compose restart grinder`.
5. Verify: alerts do not fire.

---

## Decision Tree

```
Alert fired
  |
  +-- GrinderHttpWriteDeadlineMissBurst
  |     |
  |     +-- Is exchange having issues?
  |     |     YES -> Wait for recovery. Timeouts will self-resolve.
  |     |     NO  -> Check deadline budgets. Too tight? Increase and restart.
  |     |
  |     +-- Persistent across multiple ops?
  |           YES -> Network issue or exchange degraded. Consider rollback.
  |           NO  -> Single op — adjust that op's deadline.
  |
  +-- GrinderHttpCancelLatencyP99High
  |     |
  |     +-- High-volatility event?
  |     |     YES -> Expected. Exchange is queueing. Monitor.
  |     |     NO  -> Network path issue. Check traceroute.
  |     |
  |     +-- p99 consistently > budget?
  |           YES -> Increase HTTP_DEADLINE_CANCEL_ORDER_MS.
  |           NO  -> Transient spike, will resolve.
  |
  +-- GrinderHttpReadRetriesSpike
  |     |
  |     +-- Retries succeeding?
  |     |     YES -> Degraded but functional. Monitor.
  |     |     NO  -> Also seeing fails? Check fail_total. May need rollback.
  |     |
  |     +-- Which reason dominates?
  |           timeout -> Increase read deadline.
  |           5xx     -> Exchange issue. Wait.
  |           connect -> Network issue. Check connectivity.
  |
  +-- GrinderHttp429RateLimitSpike
        |
        +-- Multiple instances sharing API key?
        |     YES -> Isolate. One key per instance.
        |     NO  -> Reduce request frequency.
        |
        +-- Retries amplifying?
              YES -> Set HTTP_MAX_ATTEMPTS_*=1 to stop retry amplification.
              NO  -> Increase RECONCILE_INTERVAL_MS.
```

---

## Evidence Bundle

When escalating or performing post-incident review, save:

| Artifact | Command |
|----------|---------|
| Metrics snapshot | `curl -sf http://localhost:9090/metrics > metrics_$(date +%s).txt` |
| HTTP-specific metrics | `curl -sf http://localhost:9090/metrics \| grep -E 'grinder_http_' > http_metrics_$(date +%s).txt` |
| Container logs | `docker logs grinder --since 30m > grinder_logs_$(date +%s).txt 2>&1` |
| Health endpoint | `curl -sf http://localhost:9090/healthz > healthz_$(date +%s).json` |
| Git SHA | `git -C /path/to/grinder rev-parse HEAD` |
| Timestamp | `date -u` |
| Env snapshot | `docker compose exec grinder env \| grep -E 'LATENCY_RETRY\|HTTP_MAX\|HTTP_DEADLINE' > env_$(date +%s).txt` |

---

## Follow-up

After resolving an HTTP latency/retry incident:

1. Review whether thresholds need tuning based on observed false-positive rate.
2. Consider adjusting alert thresholds in `monitoring/alert_rules.yml` (marked "tune after 1-2 weeks").
3. Review per-op deadline budgets in `DeadlinePolicy` defaults vs observed p95/p99.
4. Document the incident and resolution in the team's incident log.
