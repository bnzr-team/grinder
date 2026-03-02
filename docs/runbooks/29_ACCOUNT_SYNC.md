# Runbook 29: Account Sync (Launch-15)

Read-only account syncer: fetches positions + open orders from the exchange,
detects mismatches, records metrics, and optionally writes evidence artifacts.

**SSOT:** `docs/15_ACCOUNT_SYNC_SPEC.md`

---

## 1. Overview

AccountSyncer periodically fetches the exchange's actual positions and open orders,
compares them against internal state, and flags discrepancies. It is **read-only** --
it never writes to the exchange (Invariant I4).

**Mismatch rules:**

| Rule | What it detects |
|------|----------------|
| `duplicate_key` | Two positions with same `(symbol, side)` or two orders with same `order_id` |
| `ts_regression` | Snapshot timestamp older than previously accepted snapshot |
| `negative_qty` | Position qty or order qty is negative |
| `orphan_order` | Order exists on exchange but not tracked by ExecutionEngine |

---

## 2. Enablement

Account sync is **off by default** (safe-by-default). To enable:

**Option A: Config field**
```python
config = LiveEngineConfig(
    account_sync_enabled=True,  # default: False
    # ... other fields
)
```

**Option B: Environment variable**
```bash
export GRINDER_ACCOUNT_SYNC_ENABLED=1  # truthy: 1, true, yes, on
```

Both require an `AccountSyncer` instance to be injected into `LiveEngineV0`.
If the syncer instance is missing, sync is silently skipped (debug log emitted).

---

## 3. Evidence artifacts

Evidence writing is **off by default**. To enable:

```bash
export GRINDER_ACCOUNT_SYNC_EVIDENCE=1  # truthy: 1, true, yes, on
```

Artifacts are written to:
```
${GRINDER_ARTIFACT_DIR:-.artifacts}/account_sync/<YYYYMMDDTHHMMSSZ>/
```

**Files:**
```
account_snapshot.json     # Full AccountSnapshot (canonical JSON)
positions.json            # Positions only
open_orders.json          # Open orders only
mismatches.json           # Detected mismatches (empty array if clean)
summary.txt               # Human-readable evidence block
sha256sums.txt            # sha256 of all artifact files
```

---

## 4. Metrics

All metrics are exposed via the Prometheus `/metrics` endpoint through `MetricsBuilder`.

| Metric | Type | Description |
|--------|------|-------------|
| `grinder_account_sync_last_ts` | gauge | Unix ms of last successful sync |
| `grinder_account_sync_age_seconds` | gauge | Seconds since last successful sync |
| `grinder_account_sync_errors_total{reason=...}` | counter | Sync errors by reason |
| `grinder_account_sync_mismatches_total{rule=...}` | counter | Mismatches by rule |
| `grinder_account_sync_positions_count` | gauge | Positions in last snapshot |
| `grinder_account_sync_open_orders_count` | gauge | Open orders in last snapshot |
| `grinder_account_sync_pending_notional` | gauge | Total notional of open orders |

---

## 5. Verifying

### Check metrics after sync

```bash
curl -s localhost:9090/metrics | grep grinder_account_sync
```

Expected output (example):
```
grinder_account_sync_last_ts 1708000000000
grinder_account_sync_age_seconds 5.23
grinder_account_sync_errors_total{reason="none"} 0
grinder_account_sync_mismatches_total{rule="none"} 0
grinder_account_sync_positions_count 2
grinder_account_sync_open_orders_count 4
grinder_account_sync_pending_notional 1250.00
```

### Verify evidence artifacts

```bash
cd .artifacts/account_sync/<timestamp>/
sha256sum -c sha256sums.txt
cat summary.txt
```

---

## 6. Alert Triage

### AccountSyncStale

**Severity:** Warning | **Category:** correctness | **`for`:** 2m

**Meaning:** Account sync data has not refreshed for >120 seconds, despite sync having
been active at some point (`last_ts > 0`).

**Impact:** Position and open-order views are stale. Mismatch detection is blind.
If an orphan order appears during this window, no alert will fire until sync resumes.

**PromQL:**
```promql
grinder_account_sync_last_ts > 0
and
grinder_account_sync_age_seconds > 120
```

**Triage Steps:**

1. Check sync age and recent errors:
   ```bash
   curl -s localhost:9090/metrics | grep grinder_account_sync
   ```
   - `age_seconds` climbing → sync loop not running or fetch failing
   - `errors_total` incrementing → fetch failures (check `reason` label)
   - `last_ts` = 0 → sync was never active (alert should not fire; check `last_ts > 0` guard)

2. Check if sync is enabled:
   ```bash
   echo $GRINDER_ACCOUNT_SYNC_ENABLED   # must be 1/true/yes/on
   ```

3. Check logs for fetch errors:
   ```bash
   grep -i "account sync" /var/log/grinder/app.log | tail -20
   ```

4. Check exchange API connectivity:
   ```bash
   curl -s localhost:9090/metrics | grep grinder_http_request
   ```

**Resolution:**
- Sync disabled: enable `GRINDER_ACCOUNT_SYNC_ENABLED=1` or silence alert
- Fetch errors: check API keys, rate limits, network connectivity
- Process stuck: restart gracefully (`kill -TERM <pid>`)

---

### AccountSyncErrors

**Severity:** Warning | **Category:** availability | **`for`:** 5m

**Meaning:** One or more account sync fetch attempts failed in the last 5 minutes.
The `reason` label indicates the error type (e.g. `TimeoutError`, `HTTPError`, `ValueError`).

**Impact:** While errors persist, the account snapshot is stale. If errors continue for >120s,
`AccountSyncStale` will also fire.

**PromQL:**
```promql
sum(increase(grinder_account_sync_errors_total{reason!="none"}[5m])) > 0
```

**Triage Steps:**

1. Identify the error reason:
   ```bash
   curl -s localhost:9090/metrics | grep grinder_account_sync_errors_total
   ```

2. Check logs for the specific error:
   ```bash
   grep -i "account sync fetch failed" /var/log/grinder/app.log | tail -10
   ```

3. If `reason=TimeoutError`: exchange API latency spike — check `grinder_http_request_duration_seconds`
4. If `reason=HTTPError`: API returned non-200 — check response codes
5. If `reason=ValueError`: response parsing failed — check if Binance API changed format

**Resolution:**
- Transient network issue: wait for self-recovery (errors counter will stop incrementing)
- Persistent errors: check API keys, IP whitelist, rate limits
- Parse errors: file a bug — exchange response format may have changed

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No sync metrics appear | Feature flag off | Set `account_sync_enabled=True` or `GRINDER_ACCOUNT_SYNC_ENABLED=1` |
| `sync_errors_total` incrementing | Exchange fetch failing | Check API keys, network, rate limits |
| `mismatches_total{rule="orphan_order"}` | Order on exchange not in ExecutionEngine | Check if manual order was placed outside the system |
| `mismatches_total{rule="ts_regression"}` | Clock skew or stale API response | Investigate exchange API latency |
| `mismatches_total{rule="duplicate_key"}` | Bug in fetch/parse layer | File a bug -- this should not happen |
| `mismatches_total{rule="negative_qty"}` | Bug in exchange response parsing | File a bug -- qty must be >= 0 |
| No evidence files written | Evidence env var not set | Set `GRINDER_ACCOUNT_SYNC_EVIDENCE=1` |

---

## 8. Architecture

```
LiveEngineV0.process_snapshot()
  |
  |- PaperEngine (grid decisions)
  |- FSM tick
  |- AccountSyncer.sync()       <-- read-only, gated by feature flag
  |    |- ExchangePort.fetch_account_snapshot()
  |    |- _detect_mismatches()
  |    |- metrics.record_sync() / record_mismatch()
  |    |- evidence.write_evidence_bundle()  (env-gated)
  |
  |- Safety gates (arming, mode, kill-switch, ...)
  |- SOR routing
  |- Exchange execution
```
