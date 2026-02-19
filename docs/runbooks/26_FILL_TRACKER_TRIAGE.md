# Runbook 26: Fill Tracker Triage (Launch-06)

## Overview

The FillTracker records fill events and emits Prometheus counters:
- `grinder_fills_total{source,side,liquidity}` — fill count
- `grinder_fill_notional_total{source,side,liquidity}` — cumulative notional
- `grinder_fill_fees_total{source,side,liquidity}` — cumulative fees

Health / operational metrics (PR3):
- `grinder_fill_ingest_polls_total{source}` — ingest poll iterations
- `grinder_fill_ingest_enabled{source}` — 1 if ingest is on, 0 otherwise
- `grinder_fill_ingest_errors_total{source,reason}` — errors (http/parse/cursor/unknown)
- `grinder_fill_cursor_load_total{source,result}` — cursor load ok/error
- `grinder_fill_cursor_save_total{source,result}` — cursor save ok/error/rejected_non_monotonic

Cursor stuck detection (PR6):
- `grinder_fill_cursor_last_save_ts{source}` — epoch seconds of last successful cursor save
- `grinder_fill_cursor_age_seconds{source}` — seconds since last save (computed at scrape time)

**Labels**: `source` (reconcile/sim/manual/none), `side` (buy/sell/none), `liquidity` (maker/taker/none), `reason` (http/parse/cursor/unknown), `result` (ok/error/rejected_non_monotonic).

**Current state (Launch-06 PR6)**: FillTracker is wired into the reconcile loop with health metrics and alert rules. When `FILL_INGEST_ENABLED=1`, each reconcile iteration fetches `userTrades` from Binance, ingests them into FillTracker, and pushes counters to FillMetrics. A persistent cursor file (`FILL_CURSOR_PATH`) prevents re-reading trades after restarts. The cursor is saved on every successful poll (not just when new trades arrive), keeping `cursor_age_seconds` fresh during quiet markets. A monotonicity guard rejects backward cursor writes using tuple comparison `(last_trade_id, last_ts_ms)`.

---

## Safety invariants

These invariants MUST hold for any PR touching fill ingest code:

1. **metrics-out artifact MUST include `grinder_fill_*` alongside reconcile metrics.** The `--metrics-out` file writes both `metrics.to_prometheus_lines()` and `fill_metrics.to_prometheus_lines()`.

2. **Zero write ops in fill code.** Fill ingestion is read-only. Verify with:

   ```bash
   rg -n 'place_order|cancel_order|replace_order' \
     src/grinder/execution/fill_cursor.py \
     src/grinder/execution/fill_ingest.py \
     src/grinder/observability/fill_metrics.py
   ```

3. **Broader write-op grep** (for PRs touching REST call sites):

   On PR diff (preferred — catches new write ops introduced by the PR):

   ```bash
   git diff main...HEAD -- src/grinder scripts | \
     rg -n '(POST|PUT|DELETE).*/order|newOrder|batchOrders|cancelOrder|cancelReplace'
   ```

   On working tree (belt-and-suspenders — matches all existing write-op sites):

   ```bash
   rg -n '/fapi/v1/order|newOrder|batchOrders|cancelReplace' \
     --type py src/grinder/execution src/grinder/net scripts
   ```

   Expected matches: only in `binance_futures_port.py` (place/cancel/replace methods) and `binance_port.py` (spot). Zero matches in `fill_cursor.py`, `fill_ingest.py`, `fill_metrics.py`.

   Note: `--type py` avoids false positives from docs/comments. For repo-wide check without type filter, add `--glob '!docs/**'`.

4. **No forbidden labels** (`symbol=`, `order_id=`, `client_id=`, `trade_id=`) in fill metrics output.

5. **metrics-out artifact assertion** (automated in `smoke_fill_ingest_staging.sh`): every `--metrics-out` file must contain `grinder_fill_*` lines. Catches the silent regression where only reconcile metrics were written.

---

## Alert rules

| Alert | Severity | Condition | Meaning |
|-------|----------|-----------|---------|
| FillIngestDisabled | warning | `enabled < 1` for 5m | Ingest not active (expected in staging-off) |
| FillIngestNoPolls | page | `polls == 0` for 10m AND `enabled == 1` | Ingest enabled but loop stuck |
| FillCursorSaveErrors | page | `cursor_save{result="error"} > 0` in 10m | Dedup at risk after restart |
| FillParseErrors | warning | `errors{reason="parse"} > 0` in 10m | Binance schema drift |
| FillIngestHttpErrors | warning | `errors{reason="http"} > 3` in 10m | Network/API issues |
| FillCursorStuck | warning | `cursor_age > 1800s` AND `enabled == 1` AND `polls > 0` AND no save errors/rejects in 30m | Cursor not saved for 30+ min despite active polling |
| FillCursorNonMonotonicRejected | warning | `cursor_save{result="rejected_non_monotonic"} > 0` in 30m | Backward cursor write rejected (data corruption or logic bug) |

---

## Enablement procedure

### Recommended cursor path

```
FILL_CURSOR_PATH=/var/lib/grinder/fill_cursor.json
```

### Docker Compose snippet

```yaml
services:
  grinder:
    environment:
      - FILL_INGEST_ENABLED=1
      - FILL_CURSOR_PATH=/var/lib/grinder/fill_cursor.json
    volumes:
      - grinder_state:/var/lib/grinder
volumes:
  grinder_state:
```

---

## Gate 0 — Baseline (OFF)

`FILL_INGEST_ENABLED` unset or `"0"`.

```bash
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest
# Expected:
#   grinder_fill_ingest_enabled{source="reconcile"} 0
#   grinder_fill_ingest_polls_total{source="none"} 0
#   grinder_fill_ingest_errors_total{source="none",reason="none"} 0
```

Fill event counters show placeholders at zero:

```bash
curl -sf http://localhost:9090/metrics | grep grinder_fills_total
# grinder_fills_total{source="none",side="none",liquidity="none"} 0
```

---

## Gate 1 — Enable (ON) dry-run

```bash
export FILL_INGEST_ENABLED=1
export FILL_CURSOR_PATH=/var/lib/grinder/fill_cursor.json
docker-compose up -d --force-recreate
sleep 20
```

Verify:

```bash
# Polls are growing
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest_polls_total
# grinder_fill_ingest_polls_total{source="reconcile"} >0

# Enabled gauge is 1
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest_enabled
# grinder_fill_ingest_enabled{source="reconcile"} 1

# No errors
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest_errors_total
# grinder_fill_ingest_errors_total{source="none",reason="none"} 0  (or no error lines)

# Events growing (if trades exist on account)
curl -sf http://localhost:9090/metrics | grep grinder_fills_total
# grinder_fills_total{source="reconcile",side="buy",liquidity="taker"} >0

# Cursor file exists and changes
ls -la /var/lib/grinder/fill_cursor.json
cat /var/lib/grinder/fill_cursor.json
# {"last_trade_id": ..., "last_ts_ms": ..., "updated_at_ms": ...}

# Cursor save OK
curl -sf http://localhost:9090/metrics | grep grinder_fill_cursor_save_total
# grinder_fill_cursor_save_total{source="reconcile",result="ok"} >0
```

---

## Gate 2 — Restart safety

```bash
docker-compose restart grinder
sleep 20
```

Verify:

```bash
# Polls continue growing (no reset to zero — Prometheus scrapes cumulative)
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest_polls_total

# No "wild jump" in fills (cursor prevents re-ingestion)
curl -sf http://localhost:9090/metrics | grep grinder_fills_total

# Cursor file still present
ls -la /var/lib/grinder/fill_cursor.json

# Cursor load OK after restart
curl -sf http://localhost:9090/metrics | grep grinder_fill_cursor_load_total
# grinder_fill_cursor_load_total{source="reconcile",result="ok"} >0
```

---

## Rollback

```bash
unset FILL_INGEST_ENABLED
docker-compose up -d --force-recreate
sleep 20
```

Verify:

```bash
# Enabled gauge is 0
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest_enabled
# grinder_fill_ingest_enabled{source="reconcile"} 0

# Polls stop growing
curl -sf http://localhost:9090/metrics | grep grinder_fill_ingest_polls_total
# Value frozen (no new increments)

# Fill counters frozen (not reset — Prometheus keeps last value)
curl -sf http://localhost:9090/metrics | grep grinder_fills_total
```

---

## Quiet market semantics

When the market is quiet (no new fills), this is **normal and expected**:

| Metric | Quiet market behavior | Meaning |
|--------|----------------------|---------|
| `grinder_fill_ingest_polls_total` | **Increases** | Reconcile loop is running, polling API |
| `grinder_fill_ingest_enabled` | **1** (if ON) | Feature is active |
| `grinder_fills_total` | **Unchanged** | No new fills to count |
| `grinder_fill_notional_total` | **Unchanged** | No new notional |
| `grinder_fill_cursor_save_total` | **Increases every poll** | Cursor saved every poll (PR6), `updated_at_ms` always fresh |
| `grinder_fill_cursor_load_total` | **Increments on restart only** | Cursor loaded once at startup |
| `grinder_fill_cursor_last_save_ts` | **Updates every poll** | Epoch seconds of last save — stays fresh |
| `grinder_fill_cursor_age_seconds` | **Low** (<30s typical) | Seconds since last save — stays low during quiet markets |

**What pages and what doesn't:**

- **Pages**: No polls while `enabled==1` (FillIngestNoPolls) — loop is stuck or HTTP dead. Cursor save errors (FillCursorSaveErrors) — data loss risk.
- **Warns**: HTTP errors > 3 (FillIngestHttpErrors) — degraded connectivity. Parse errors (FillParseErrors) — Binance schema drift. Cursor stuck (FillCursorStuck) — saves not happening. Non-monotonic cursor rejected (FillCursorNonMonotonicRejected) — data corruption.
- **Does NOT page**: Quiet market (no new fills). Ingest disabled (FillIngestDisabled = warning only).

**Key insight**: Polls growing + fills not growing = quiet market (safe). Polls NOT growing + enabled = 1 = problem (page).

### NoPolls vs QuietMarket vs CursorStuck

| Condition | enabled | polls growing? | cursor_age_seconds | Diagnosis |
|-----------|:---:|:---:|:---:|:--|
| Feature OFF | 0 | No | N/A | Normal |
| NoPolls | 1 | **No** | Growing | Loop stuck — pages |
| Quiet Market | 1 | Yes | **Low** (<30s) | Normal — cursor saved every poll |
| CursorStuck | 1 | Yes | **High** (>1800s) | Save failing — warns |

**Note on `cursor_age_seconds == 0` with `enabled == 1`**: This means no successful save/poll has occurred yet (startup state). Check NoPolls and HttpErrors first — the loop may not have completed its first iteration.

### Thresholds

| Metric | Default threshold | Notes |
|--------|-------------------|-------|
| FillCursorStuck | `cursor_age > 1800s` (30 min) | Valid when reconcile interval ≤ 5m. For reconcile ≥ 10m, adjust to ≥ 6× interval. |
| FillCursorNonMonotonicRejected | any `> 0` in 30m | Any backward cursor write is unexpected |
| FillCursorSaveErrors | any `> 0` in 10m | Pages — disk/permission issue |
| FillIngestNoPolls | `polls == 0` for 10m | Pages — loop stuck |

---

## What "good" looks like

### Feature OFF (FILL_INGEST_ENABLED unset or "0")

Placeholder counters at zero:

```
grinder_fills_total{source="none",side="none",liquidity="none"} 0
grinder_fill_notional_total{source="none",side="none",liquidity="none"} 0
grinder_fill_fees_total{source="none",side="none",liquidity="none"} 0
grinder_fill_ingest_enabled{source="reconcile"} 0
```

### Feature ON (FILL_INGEST_ENABLED=1) with trades

Counters increment with real labels:

```
grinder_fills_total{source="reconcile",side="buy",liquidity="taker"} 42
grinder_fill_notional_total{source="reconcile",side="buy",liquidity="taker"} 12345.67
grinder_fill_fees_total{source="reconcile",side="buy",liquidity="taker"} 4.93
grinder_fill_ingest_polls_total{source="reconcile"} 120
grinder_fill_ingest_enabled{source="reconcile"} 1
grinder_fill_cursor_save_total{source="reconcile",result="ok"} 120
grinder_fill_cursor_last_save_ts{source="reconcile"} 1700000120.000
grinder_fill_cursor_age_seconds{source="reconcile"} 5.123
```

---

## Triage decision tree

### Metrics missing entirely

```bash
curl -sf http://localhost:9090/metrics | grep grinder_fill
```

If no output:
1. Check that grinder container is running: `docker-compose ps`
2. Check `/healthz`: `curl -sf http://localhost:9090/healthz`
3. Check that Launch-06 PR3 is deployed (code includes fill health metrics)

### Counters still at zero (placeholder only)

1. Check `FILL_INGEST_ENABLED` is set to `"1"` in the environment.
2. Check `grinder_fill_ingest_enabled{source="reconcile"}` gauge — should be `1`.
3. Check reconcile loop is actually running: `grinder_reconcile_runs_total` > 0.
4. If no trades on the account, counters stay at zero (correct behavior).

### Ingest polls not growing

1. Check `grinder_fill_ingest_enabled` is 1.
2. Check reconcile loop: `increase(grinder_reconcile_runs_total[5m])`.
3. Check for errors: `grinder_fill_ingest_errors_total`.

### Counters growing but values seem wrong

1. Check fill source: `source=` label tells you where fills come from
2. Compare `buy` vs `sell` notional — should be roughly balanced for grid strategy
3. Compare `maker` vs `taker` — high taker ratio may indicate aggressive fills
4. Check fees: `grinder_fill_fees_total` should be small relative to notional

### Sudden spike in fill count

1. Check if reconcile loop ran: `grinder_reconcile_runs_total`
2. Check for market volatility (external)
3. If fills are from `source="sim"`, this is paper trading (no real money)

### Cursor issues

If fills seem to repeat after restart or skip trades:

1. Check cursor file: `cat $FILL_CURSOR_PATH`
2. Verify `last_trade_id` matches expected Binance trade ID
3. Check cursor save metric: `grinder_fill_cursor_save_total{result="error"}`
4. Check cursor load metric: `grinder_fill_cursor_load_total{result="error"}`
5. Check file permissions: `ls -la $FILL_CURSOR_PATH`
6. To reset cursor: delete file and restart (`rm $FILL_CURSOR_PATH`)

### Cursor not writing

1. Check `grinder_fill_cursor_save_total{result="error"}` — if > 0, disk issue.
2. Check directory exists and is writable: `ls -la $(dirname $FILL_CURSOR_PATH)`
3. Check disk space: `df -h $(dirname $FILL_CURSOR_PATH)`

### FillCursorStuck

The cursor has not been saved for 30+ minutes despite active polling.

**First check** (before anything else):
1. `grinder_fill_cursor_save_total{source="reconcile",result="error"}` — if non-zero, that's the root cause (disk/permissions). See "Cursor not writing" above.
2. `grinder_fill_cursor_save_total{source="reconcile",result="rejected_non_monotonic"}` — if non-zero, the monotonicity guard is rejecting writes. See "FillCursorNonMonotonicRejected" below.

If both are zero (genuine stuck):
3. Check logs for `FILL_CURSOR` entries: `docker logs grinder --since 30m 2>&1 | grep FILL_CURSOR`
4. Check cursor file permissions: `ls -la $FILL_CURSOR_PATH`
5. Check disk space: `df -h $(dirname $FILL_CURSOR_PATH)`
6. Check if the reconcile loop is reaching the save point: `grinder_fill_ingest_polls_total` should be growing.

**Note**: This alert is suppressed when `cursor_save{result=~"error|rejected_non_monotonic"}` is growing — those have their own alerts (FillCursorSaveErrors, FillCursorNonMonotonicRejected).

### FillCursorNonMonotonicRejected

A cursor write was rejected because the new `(trade_id, ts_ms)` tuple is less than the existing cursor file.

**Cursor monotonicity key**: `(last_trade_id, last_ts_ms)` — lexicographic tuple comparison. We assume `trade_id` does not regress within a single account; the monotonicity guard protects against cursor regression regardless of external ordering guarantees.

Triage:
1. Check cursor file: `cat $FILL_CURSOR_PATH` — note the current `last_trade_id` and `last_ts_ms`.
2. Check logs for `FILL_CURSOR_REJECTED_NON_MONOTONIC` — logs include `existing_key` and `new_key` (integer tuples, safe to log).
3. Possible causes:
   - **Cursor file manually edited** with a future value
   - **Clock skew** between processes writing the same cursor file
   - **Logic bug** in ingest pipeline producing backward trade IDs
4. To recover: delete the cursor file and restart. This will re-read recent trades (dedup in FillTracker handles duplicates).

**Logging safety**: Extra fields logged are `path` (filename), `existing_key`/`new_key` (integer tuples). No secrets, no file contents, no API keys.

---

## Evidence bundle commands

```bash
# 1. All fill metrics (health + event counters)
curl -sf http://localhost:9090/metrics | grep grinder_fill

# 2. Health check
curl -sf http://localhost:9090/healthz | python3 -m json.tool

# 3. Fill health metrics specifically
curl -sf http://localhost:9090/metrics | grep -E 'grinder_fill_(ingest|cursor)'

# 4. Check for forbidden labels
curl -sf http://localhost:9090/metrics | grep grinder_fill | grep -E 'symbol=|order_id=|client_id=' || echo "CLEAN: no forbidden labels"

# 5. Cursor file state
cat $FILL_CURSOR_PATH 2>/dev/null || echo "No cursor file"

# 6. Recent container logs
docker logs grinder --since 5m 2>&1 | tail -30

# 7. Reconcile state (for context)
curl -sf http://localhost:9090/metrics | grep grinder_reconcile_runs_total

# 8. Full metrics snapshot (for archival)
curl -sf http://localhost:9090/metrics > /tmp/metrics_snapshot_$(date +%s).txt
```

---

## Companion scripts

| Script | Purpose | API keys needed |
|--------|---------|-----------------|
| `scripts/smoke_fill_ingest.sh` | Local CI smoke (FakePort) — validates metric wiring | No |
| `scripts/smoke_fill_ingest_staging.sh` | Staging dry-run — validates real Binance reads + cursor persistence | Yes |

Both scripts print PASS/FAIL and exit non-zero on failure.
