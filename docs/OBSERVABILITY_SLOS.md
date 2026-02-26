# Observability SLO Registry

Unified index of Service Level Objectives for the grinder trading system.
Individual SLOs are defined in their respective runbooks; this document
provides a single-page reference for on-call operators and incident response.

---

## Quick Reference: SLO Table

| # | SLO | Target | PromQL (check) | Alert | Dashboard | Runbook |
|---|-----|--------|-----------------|-------|-----------|---------|
| 1 | Metrics scrape | up=1, 100% | `up{job="grinder"}` | `GrinderTargetDown` | Prometheus Targets | [02_HEALTH_TRIAGE.md](#1-metrics-scrape-availability) |
| 2 | Readyz readiness | HTTP 200 within 5m of start | `grinder_readyz_ready` | `ReadyzNotReady` | Trading Loop: Readyz Ready | [02_HEALTH_TRIAGE.md#readyz-not-ready](runbooks/02_HEALTH_TRIAGE.md#readyz-not-ready) |
| 3 | Engine initialized | init=1 within 5m of start | `grinder_live_engine_initialized` | `EngineInitDown` | Trading Loop: Engine Initialized | [02_HEALTH_TRIAGE.md#engine-initialization-failures](runbooks/02_HEALTH_TRIAGE.md#engine-initialization-failures) |
| 4 | Fill-prob block rate | <10 blocks/5m sustained | `increase(grinder_router_fill_prob_blocks_total[5m])` | `FillProbBlocksHigh` | Trading Loop: Fill-Prob Blocks | [31_FILL_PROB_ROLLOUT.md](runbooks/31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) |
| 5 | Reconcile freshness | snapshot age <120s | `grinder_reconcile_last_snapshot_age_seconds` | `ReconcileSnapshotStale` | Reconcile: Snapshot Age | [16_RECONCILE_ALERTS_SLOS.md](runbooks/16_RECONCILE_ALERTS_SLOS.md#reconcilesnapshotstale) |

**Related SLOs** (detailed in their own runbooks):

| SLO | Target | Runbook |
|-----|--------|---------|
| ML inference p99 latency | <100ms | [18_ML_INFERENCE_SLOS.md](runbooks/18_ML_INFERENCE_SLOS.md#mlinferencelatencyhigh) |
| ML inference error rate | <5% | [18_ML_INFERENCE_SLOS.md](runbooks/18_ML_INFERENCE_SLOS.md#mlinferenceerrorratehigh) |
| Reconcile loop availability | >0 runs/5m | [16_RECONCILE_ALERTS_SLOS.md](runbooks/16_RECONCILE_ALERTS_SLOS.md#reconcileloopdown) |
| Account sync freshness | age <120s | [29_ACCOUNT_SYNC.md](runbooks/29_ACCOUNT_SYNC.md#accountsyncstale) |
| Fill cursor freshness | age <1800s | [26_FILL_TRACKER_TRIAGE.md](runbooks/26_FILL_TRACKER_TRIAGE.md#fillcursorstuck) |

---

## SLO Details

### 1. Metrics Scrape Availability

**What:** Prometheus can reach the grinder `/metrics` endpoint.

**Why critical:** If scraping fails, all other alerts go silent. This is the
"meta-SLO" — without it, nothing else works.

**PromQL:**
```promql
up{job="grinder"} == 0    # fires GrinderTargetDown after 1m
```

**Alert:** `GrinderTargetDown` (critical, 1m)

**Dashboard:** Prometheus UI > Targets page (`http://localhost:9091/targets`)

**What to check if firing:**
1. Is the process alive? `pgrep -af 'grinder|run_trading'`
2. Is port 9090 listening? `ss -lntp | grep :9090`
3. Can you reach metrics? `curl -sS http://localhost:9090/metrics | head -5`
4. Docker running? `docker ps | grep grinder`
5. Firewall? Network policy blocking Prometheus → grinder?

**See also:** [02_HEALTH_TRIAGE.md](runbooks/02_HEALTH_TRIAGE.md) — "First look" section.

---

### 2. Readyz Readiness

**What:** The `/readyz` endpoint returns HTTP 200 (trading loop is ready).

**Why critical:** A not-ready process won't process ticks or execute orders.
Normal during HA standby; investigate if unexpected.

**PromQL:**
```promql
grinder_readyz_callback_registered == 1
  and grinder_readyz_ready == 0
  and grinder_up == 1
```

**Alert:** `ReadyzNotReady` (warning, 5m)

**Dashboard:** Trading Loop > Readyz Ready (stat panel)

**Quick check:**
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/readyz
# 200 = ready, 503 = not ready
```

**See also:** [02_HEALTH_TRIAGE.md#readyz-not-ready](runbooks/02_HEALTH_TRIAGE.md#readyz-not-ready)

---

### 3. Engine Initialized

**What:** The trading engine completed initialization (`grinder_live_engine_initialized == 1`).

**Why critical:** Process may be up but not processing ticks if init failed
(e.g., fill model load failure, missing config).

**PromQL:**
```promql
grinder_live_engine_initialized == 0
  and grinder_up == 1
```

**Alert:** `EngineInitDown` (critical, 5m)

**Dashboard:** Trading Loop > Engine Initialized (stat panel)

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep grinder_live_engine_initialized
# expect: grinder_live_engine_initialized 1
```

**See also:** [02_HEALTH_TRIAGE.md#engine-initialization-failures](runbooks/02_HEALTH_TRIAGE.md#engine-initialization-failures)

---

### 4. Fill-Prob Block Rate

**What:** The fill probability model is not blocking an excessive number of orders.

**Why critical:** Heavy blocking (>=10 blocks/5m) means the model is cutting
order flow significantly. Circuit breaker may auto-trip to bypass the gate.

**PromQL:**
```promql
increase(grinder_router_fill_prob_blocks_total[5m]) >= 10    # FillProbBlocksHigh
increase(grinder_router_fill_prob_cb_trips_total[5m]) > 0     # CB tripped
```

**Alerts:**
- `FillProbBlocksSpike` (warning) — any blocks sustained for 5m
- `FillProbBlocksHigh` (critical) — >=10 blocks in 5m
- `FillProbCircuitBreakerTripped` (warning) — CB auto-bypassed gate

**Dashboard:** Trading Loop > Fill-Prob Blocks & CB Trips (timeseries)

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep -E 'router_fill_prob_(blocks|cb_trips|enforce|auto_threshold)'
```

**See also:** [31_FILL_PROB_ROLLOUT.md](runbooks/31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning)

---

### 5. Reconcile Freshness

**What:** The reconciliation snapshot is fresh (age < 120 seconds).

**Why critical:** Stale snapshots mean the system is operating on outdated
position/order data, which can lead to incorrect remediation decisions.

**PromQL:**
```promql
grinder_reconcile_last_snapshot_age_seconds > 120    # ReconcileSnapshotStale
```

**Alert:** `ReconcileSnapshotStale` (warning, 2m)

**Dashboard:** Reconcile > Snapshot Age (timeseries)

**Quick check:**
```bash
curl -fsS http://localhost:9090/metrics | grep grinder_reconcile_last_snapshot_age_seconds
# expect: < 120
```

**See also:** [16_RECONCILE_ALERTS_SLOS.md#reconcilesnapshotstale](runbooks/16_RECONCILE_ALERTS_SLOS.md#reconcilesnapshotstale)

---

## Alert-to-Dashboard Mapping

| Alert Group | Dashboard (uid) | Key Panels |
|-------------|-----------------|------------|
| `grinder_availability` | Prometheus Targets | up/down status |
| `grinder_health` | Trading Loop (`grinder-trading-loop`) | Engine Init, Readyz, Futures HTTP |
| `grinder_risk` | Overview (`grinder-overview`) | Kill-switch, Drawdown |
| `grinder_gating` | Overview (`grinder-overview`) | Allowed/Blocked counters |
| `grinder_fill_prob_cb` | Trading Loop (`grinder-trading-loop`) | Fill-Prob Blocks, CB Trips |
| `grinder_reconcile` | Reconcile (`grinder-reconcile`) | Snapshot Age, Mismatches, Actions |
| `grinder_ml_inference` | ML Overview (`grinder-ml-overview`) | Latency, Error Rate, Active Mode |
| `grinder_data_quality` | Overview (`grinder-overview`) | Stale/Gap/Outlier counters |
| `grinder_http_latency` | Trading Loop (`grinder-trading-loop`) | Route+Method panels |
| `grinder_fill_health` | Overview (`grinder-overview`) | Ingest polls, Cursor age |
| `grinder_fsm` | Overview (`grinder-overview`) | FSM state |
| `grinder_sor` | Overview (`grinder-overview`) | Router decisions |
| `grinder_account_sync` | Overview (`grinder-overview`) | Sync age, Errors |

---

## Severity Reference

| Severity | Meaning | Response Time | Examples |
|----------|---------|---------------|----------|
| `critical` | Immediate action required | <5 min | GrinderDown, EngineInitDown, KillSwitchTripped |
| `page` | Wake-up worthy | <15 min | GrinderDataStaleBurst, FillCursorSaveErrors |
| `warning` | Investigate soon | <1 hour | ReadyzNotReady, HighDrawdown, ReconcileSnapshotStale |
| `ticket` | Track and fix | Next business day | GrinderDataGapSpike, GrinderHttp429RateLimitSpike |
| `info` | Informational | Review in batch | GrinderRecentRestart, SorNoopSpike |

---

## Metrics SSOT

All metrics are defined in:
- **Contract:** `src/grinder/observability/metrics_contract.py` (270 patterns)
- **Builder:** `src/grinder/observability/metrics_builder.py`
- **Alert rules:** `monitoring/alert_rules.yml` (35 alerts)

To validate metrics exist:
```bash
python3 -m pytest tests/unit/test_smoke_metrics_contract.py -v
```
