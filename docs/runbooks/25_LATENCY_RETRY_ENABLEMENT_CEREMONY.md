# Runbook 25: Latency/Retry Enablement Ceremony

Operator procedure for safely enabling `LATENCY_RETRY_ENABLED=1` (Launch-05b).

## Purpose / Scope

- Enable `MeasuredSyncHttpClient` per-op deadlines, retries, and HTTP metrics in a live environment.
- Scope: **enablement + observation only**. No threshold tuning.
- Tune deadlines and alert thresholds only after collecting real p95/p99 data.
- Triage: [Runbook 24](24_LATENCY_RETRY_TRIAGE.md) for alert response.

**Safe-by-default:** When `LATENCY_RETRY_ENABLED` is unset or `"0"`, `MeasuredSyncHttpClient` is a pure pass-through with zero behavior change (proven by `test_disabled_is_zero_behavior_change`).

---

## Preconditions

- [ ] Launch-05 PRs merged (#180, #181, #182)
- [ ] Launch-05c PR merged (#184) -- HTTP probe for observable metrics
- [ ] Alert rules deployed (`grinder_http_latency` group in Prometheus)
- [ ] Running in **STAGING** (or SHADOW) -- do not enable directly in ACTIVE
- [ ] Access to `/metrics`, `/healthz`, `/readyz` endpoints
- [ ] `HTTP_PROBE_ENABLED=1` set (generates real HTTP traffic for observation; Launch-05c)
- [ ] Operator assigned, time window: 15-30 minutes
- [ ] Artifacts directory exists: `mkdir -p artifacts/`

---

## Conservative First-Enable Config

Start conservative. Tune after observing real data.

```bash
# Enable per-op deadlines + metrics collection
export LATENCY_RETRY_ENABLED=1           # Source: run_live_reconcile.py:409

# Read ops: allow 1 retry (2 attempts total)
export HTTP_MAX_ATTEMPTS_READ=2          # Source: run_live_reconcile.py:412, default=1

# Write ops: NO retries on first enable (most conservative)
export HTTP_MAX_ATTEMPTS_WRITE=1         # Source: run_live_reconcile.py:417, default=1

# Deadlines: use defaults from DeadlinePolicy (retry_policy.py:160-169)
# Do NOT override HTTP_DEADLINE_*_MS until you have real p95/p99 data.
#
# Default budgets:
#   cancel_order:    600ms
#   place_order:    1500ms
#   cancel_all:     1200ms
#   get_positions:  2500ms
#   get_account:    2500ms
#   get_open_orders: 2000ms
#   exchange_info:  5000ms
#   ping_time:       800ms
```

**Key principle:** Enable first, collect real latency data, then tune deadlines and alert thresholds.

---

## Pre-flight

### 1. Record baseline state

```bash
echo "=== PRE-FLIGHT SNAPSHOT ==="
git rev-parse HEAD
date -u
env | grep -E "LATENCY_RETRY_ENABLED|HTTP_MAX_ATTEMPTS|HTTP_DEADLINE_" || echo "(none set)"
```

### 2. Verify service health

```bash
# Liveness
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# Readiness
curl -sf http://localhost:9090/readyz | python3 -m json.tool

# Reconcile loop running
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total
```

### 3. Confirm no existing HTTP metrics (pre-enable baseline)

```bash
curl -sf http://localhost:9090/metrics | grep 'grinder_http_' | head -5
# Expected: empty or zero values (latency/retry not yet enabled)
```

---

## Enable

### 1. Set environment variables

Apply the config from [Conservative First-Enable Config](#conservative-first-enable-config).

### 2. Restart

```bash
docker compose restart grinder
```

### 3. First 2-3 minutes -- quick sanity

```bash
# Health still OK
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# Reconcile loop still ticking
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total
# Counter should be incrementing

# HTTP metrics starting to appear (may take a few reconcile cycles)
curl -sf http://localhost:9090/metrics | grep 'grinder_http_requests_total'
```

---

## Observation Window (15-30 min)

### Watch for page alerts (must be zero)

| Alert | Acceptable? |
|-------|-------------|
| GrinderHttpWriteDeadlineMissBurst | NO -- investigate immediately (RB24) |
| GrinderHttpCancelLatencyP99High | NO -- investigate immediately (RB24) |

### Watch for ticket alerts (acceptable if transient)

| Alert | Acceptable? |
|-------|-------------|
| GrinderHttpReadRetriesSpike | Transient OK, sustained = investigate |
| GrinderHttp429RateLimitSpike | Any firing = follow RB24 rate limit triage |

### Metrics to check periodically

With `HTTP_PROBE_ENABLED=1` (Launch-05c), expect `op="ping_time"` and `op="exchange_info"` counters to grow every probe interval (~5s).

```bash
# Request counts by op (should see non-zero for active ops)
curl -sf http://localhost:9090/metrics | grep 'grinder_http_requests_total'

# Retry counts (should be low or zero)
curl -sf http://localhost:9090/metrics | grep 'grinder_http_retries_total' | grep -v '} 0'

# Fail counts (should be zero or very low)
curl -sf http://localhost:9090/metrics | grep 'grinder_http_fail_total' | grep -v '} 0'

# Latency histogram (confirm data is flowing)
curl -sf http://localhost:9090/metrics | grep 'grinder_http_latency_ms_count'

# No forbidden labels (validator guarantees this, but belt-and-suspenders)
curl -sf http://localhost:9090/metrics \
  | grep -E "symbol=|order_id=|client_id=|key=" \
  && echo "FAIL: forbidden labels found" || echo "OK: no forbidden labels"
```

---

## Rollback

If any page alert fires or behavior is unexpected:

```bash
unset LATENCY_RETRY_ENABLED
docker compose restart grinder
```

### Verify rollback

```bash
# Health OK
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# Reconcile loop still running
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total
# Counter should still increment

# HTTP metrics stop incrementing (watch over 60s)
curl -sf http://localhost:9090/metrics | grep 'grinder_http_requests_total'
# Values should freeze (no new observations)
```

---

## Evidence Bundle

Save these artifacts after the observation window (or after rollback):

```bash
mkdir -p artifacts/launch-05b

echo "=== EVIDENCE BUNDLE ===" > artifacts/launch-05b/summary.txt
git rev-parse HEAD >> artifacts/launch-05b/summary.txt
date -u >> artifacts/launch-05b/summary.txt
env | grep -E "LATENCY_RETRY_ENABLED|HTTP_MAX_ATTEMPTS|HTTP_DEADLINE_" \
  >> artifacts/launch-05b/summary.txt 2>/dev/null || true

curl -sf http://localhost:9090/metrics > artifacts/launch-05b/metrics.prom
curl -sf http://localhost:9090/metrics \
  | grep 'grinder_http_' > artifacts/launch-05b/http_metrics.prom
curl -sf http://localhost:9090/healthz > artifacts/launch-05b/healthz.json
curl -sf http://localhost:9090/readyz > artifacts/launch-05b/readyz.json
docker compose logs grinder --tail=200 > artifacts/launch-05b/grinder_tail.log 2>&1
```

---

## Exit Criteria

- [ ] 15-30 minutes with `LATENCY_RETRY_ENABLED=1`, no page alerts
- [ ] Reconcile loop stable (runs_total incrementing normally)
- [ ] No anomalous growth in `retries_total` or `fail_total`
- [ ] Rollback tested at least once (in STAGING, before ACTIVE)
- [ ] Evidence bundle saved

---

## Next Steps (after successful enablement)

1. **Collect 1-2 weeks of data** with default deadlines.
2. **Review p95/p99** from `grinder_http_latency_ms` histogram per op.
3. **Tune deadlines** if defaults are too tight or too loose:
   ```bash
   export HTTP_DEADLINE_CANCEL_ORDER_MS=800  # Source: run_live_reconcile.py:422-426
   ```
4. **Tune alert thresholds** in `monitoring/alert_rules.yml` based on observed false-positive rate.
5. **Consider enabling write retries** (`HTTP_MAX_ATTEMPTS_WRITE=2`) after confidence is established.
6. **Graduate to ACTIVE** using [Runbook 22](22_ACTIVE_ENABLEMENT_CEREMONY.md) if not already there.
