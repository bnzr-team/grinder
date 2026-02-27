# Runbook: Alert Response

Response procedures for Prometheus alerts defined in `monitoring/alert_rules.yml`.

**SSOT references:**
- Alert routing index (quick lookup): [`ALERT_INDEX.md`](ALERT_INDEX.md)
- SLO definitions & PromQL: [`docs/OBSERVABILITY_SLOS.md`](../OBSERVABILITY_SLOS.md)
- Alert rules source: [`monitoring/alert_rules.yml`](../../monitoring/alert_rules.yml)
- Metrics contract: [`src/grinder/observability/metrics_contract.py`](../../src/grinder/observability/metrics_contract.py)

---

## ScrapeDown = all other alerts are blind

> **If `GrinderTargetDown` is firing, stop here. Fix scraping first.**

When Prometheus cannot reach `/metrics`, every other alert in this document
is effectively silenced. This is the single most important failure mode.

**Quick check:**
```bash
curl -sS http://localhost:9090/metrics | head -3
# No output or connection refused = scrape is down
```

**Diagnostics (copy-paste):**
```bash
ss -lntp | grep ':9090'                    # is port listening?
pgrep -af 'grinder|run_trading'            # is process alive?
docker ps | grep -i grinder                # running in docker?
journalctl -u grinder -n 50 --no-pager     # systemd logs
curl -sS http://localhost:9090/readyz       # readyz probe
```

**Common causes:**
1. Trading loop not started (check deployment / systemd / docker)
2. Port 9090 occupied by another process
3. Metrics endpoint on different port (check `--metrics-port` flag)
4. Firewall / network policy blocking Prometheus -> grinder

**Runbook:** [02_HEALTH_TRIAGE.md](02_HEALTH_TRIAGE.md) -- "First look" section

---

## First 60 seconds (any alert)

When paged, run these five checks in order before reading the specific alert section:

```bash
# 0. Scrape alive? (if no output, ALL alerts are blind)
curl -fsS http://localhost:9090/metrics | head -3

# 1. Readyz (200=ready, 503=not ready, connection refused=process down)
curl -s -o /dev/null -w "readyz: %{http_code}\n" http://localhost:9090/readyz

# 2. Engine initialized?
curl -fsS http://localhost:9090/metrics | grep grinder_live_engine_initialized

# 3. Fill-prob blocks / CB trips (any blocks in last 5m?)
curl -fsS http://localhost:9090/metrics | grep -E 'router_fill_prob_(blocks_total|cb_trips_total|enforce_enabled)'

# 4. Futures HTTP safety (should be 0 in rehearsal/fixture)
curl -fsS http://localhost:9090/metrics | grep 'port_http_requests_total{port="futures"'
```

| Check | Good | Bad | Next step |
|-------|------|-----|-----------|
| #0 Scrape | output visible | connection refused / empty | See [ScrapeDown](#scrapedown--all-other-alerts-are-blind) above |
| #1 Readyz | `200` | `503` or refused | [02_HEALTH_TRIAGE.md#readyz-not-ready](02_HEALTH_TRIAGE.md#readyz-not-ready) |
| #2 Engine init | `1` | `0` | [02_HEALTH_TRIAGE.md#engine-initialization-failures](02_HEALTH_TRIAGE.md#engine-initialization-failures) |
| #3 Fill-prob | blocks=0, cb_trips=0 | blocks>0 or cb_trips>0 | [31_FILL_PROB_ROLLOUT.md](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) |
| #4 Futures HTTP | counter=0 or absent | counter>0 | [02_HEALTH_TRIAGE.md#unexpected-futures-http](02_HEALTH_TRIAGE.md#unexpected-futures-http) |

**See also:** [02_HEALTH_TRIAGE.md -- First look](02_HEALTH_TRIAGE.md#first-look-60-seconds), [OBSERVABILITY_SLOS.md](../OBSERVABILITY_SLOS.md)

---

## Alert-to-Dashboard-to-Runbook mapping

Use this table to jump from any alert directly to the right dashboard panel and runbook.

### Critical / Page alerts

| Alert | Severity | Dashboard | Panels / What to check | Immediate action | Runbook |
|-------|----------|-----------|------------------------|------------------|---------|
| `GrinderTargetDown` | critical | Prometheus Targets | up/down status | Fix scraping first (see above) | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) |
| `GrinderDown` | critical | Prometheus Targets | grinder_up gauge | Check container/process, restart if needed | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) |
| `EngineInitDown` | critical | Trading Loop | Engine Initialized stat | Check logs for init errors, fill model load | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#engine-initialization-failures) |
| `KillSwitchTripped` | critical | Overview | Kill-switch gauge, Drawdown | Verify state, check trip reason labels | [04_KILL_SWITCH](04_KILL_SWITCH.md) |
| `ConsecutiveLossTrip` | critical | Overview | Consecutive losses gauge | Check GRINDER_OPERATOR_OVERRIDE | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#consecutive-loss-guard-pr-c3b) |
| `FuturesHttpRequestsDetected` | critical | Trading Loop | Futures HTTP by Route+Method | Any /fapi/ route = real Binance call | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#unexpected-futures-http) |
| `FillProbBlocksHigh` | critical | Trading Loop | Fill-Prob Blocks, CB Trips | Check threshold, model calibration | [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) |
| `MlInferenceLatencyCritical` | critical | ML Overview | Latency p99.9 | Consider model rollback or kill-switch | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferencelatencycritical) |
| `ReconcileRemediationExecuted` | critical | Reconcile | Actions Executed | Verify intended; check remediation logs | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileremediationexecuted) |
| `GrinderDataStaleBurst` | page | Overview | Stale counters | Check exchange feed, WebSocket health | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdatastaleburst) |
| `GrinderDQBlockingActive` | page | Overview | DQ gate blocks | Investigate feed quality before action | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdqblockingactive) |
| `GrinderHttpWriteDeadlineMissBurst` | page | Trading Loop | Route+Method latency | Check network latency, exchange status | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttpwritedeadlinemissburst) |
| `GrinderHttpCancelLatencyP99High` | page | Trading Loop | Route+Method latency | Check exchange API latency | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttpcancellatencyp99high) |
| `FillIngestNoPolls` | page | Overview | Ingest polls counter | Reconcile loop may be stuck | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillingestnopolls) |
| `FillCursorSaveErrors` | page | Overview | Cursor save counters | Check disk permissions, path | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillcursorsaveerrors) |

### Warning alerts

| Alert | Severity | Dashboard | Panels / What to check | Immediate action | Runbook |
|-------|----------|-----------|------------------------|------------------|---------|
| `ReadyzNotReady` | warning | Trading Loop | Readyz Ready stat | Check HA role (standby is normal) | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#readyz-not-ready) |
| `HighDrawdown` | warning | Overview | Drawdown % | Monitor; kill-switch fires at 5% | [04_KILL_SWITCH](04_KILL_SWITCH.md) |
| `ConsecutiveLossesHigh` | warning | Overview | Consecutive losses gauge | Early warning; guard trips at threshold | [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md#consecutive-loss-guard-pr-c3b) |
| `HighGatingBlocks` | warning | Overview | Allowed/Blocked counters | Check block reasons (toxicity, position limit) | -- |
| `ToxicityTriggers` | warning | Overview | Blocked by reason | Spread spike or price impact detected | -- |
| `FillProbBlocksSpike` | warning | Trading Loop | Fill-Prob Blocks | Early warning before FillProbBlocksHigh | [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) |
| `FillProbCircuitBreakerTripped` | warning | Trading Loop | CB Trips | Review model calibration, threshold | [31_FILL_PROB_ROLLOUT](31_FILL_PROB_ROLLOUT.md#circuit-breaker-tuning) |
| `ReconcileLoopDown` | warning | Reconcile | Runs counter | Loop stopped while process alive | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileloopdown) |
| `ReconcileSnapshotStale` | warning | Reconcile | Snapshot Age | Data may be outdated (>120s) | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilesnapshotstale) |
| `ReconcileMismatchSpike` | warning | Reconcile | Mismatches counter | Investigate expected vs observed | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilemismatchspike) |
| `ReconcileMismatchNoBlocks` | warning | Reconcile | Actions, Blocks | Gates may be bypassed | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilemismatchnoblocks) |
| `ReconcileBudgetCallsExhausted` | warning | Reconcile | Budget panels | No remediation calls left today | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilebudgetcallsexhausted) |
| `ReconcileBudgetNotionalLow` | warning | Reconcile | Budget panels | Notional <10 USDT remaining | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcilebudgetnotionallow) |
| `MlInferenceLatencyHigh` | warning | ML Overview | Latency p99 | Check ONNX model performance | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferencelatencyhigh) |
| `MlInferenceErrorRateHigh` | warning | ML Overview | Error Rate | Check model and input data | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferenceerrorratehigh) |
| `MlInferenceStalled` | warning | ML Overview | Inference counter | No inferences for 10m; check config | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlinferencestalled) |
| `FsmBadStateTooLong` | warning | Overview | FSM state | Check operator override | [27_FSM_OPERATOR_OVERRIDE](27_FSM_OPERATOR_OVERRIDE.md#fsmbadstatetoolong) |
| `FsmActionBlockedSpike` | warning | Overview | FSM state | Intents rejected by permission matrix | [27_FSM_OPERATOR_OVERRIDE](27_FSM_OPERATOR_OVERRIDE.md#fsmactionblockedspike) |
| `SorBlockedSpike` | warning | Overview | Router decisions | Orders rejected by SOR | [28_SOR_FIRE_DRILL](28_SOR_FIRE_DRILL.md#sorblockedspike) |
| `AccountSyncStale` | warning | Overview | Sync age | Positions/orders may be outdated | [29_ACCOUNT_SYNC](29_ACCOUNT_SYNC.md#accountsyncstale) |
| `AccountSyncErrors` | warning | Overview | Sync errors | Check API connectivity, credentials | [29_ACCOUNT_SYNC](29_ACCOUNT_SYNC.md#accountsyncerrors) |
| `AccountSyncMismatchSpike` | warning | Overview | Sync mismatches | Expected vs observed diverged | [30_ACCOUNT_SYNC_FIRE_DRILL](30_ACCOUNT_SYNC_FIRE_DRILL.md#accountsyncmismatchspike) |
| `FillIngestDisabled` | warning | Overview | Ingest enabled gauge | Set FILL_INGEST_ENABLED=1 | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillingestdisabled) |
| `FillParseErrors` | warning | Overview | Ingest errors | Check for API schema changes | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillparseerrors) |
| `FillIngestHttpErrors` | warning | Overview | Ingest errors | Check Binance connectivity, API key | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillingesthttperrors) |
| `FillCursorStuck` | warning | Overview | Cursor age | Cursor not saved for 30m+ | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillcursorstuck) |
| `FillCursorNonMonotonicRejected` | warning | Overview | Cursor save counters | Possible data corruption | [26_FILL_TRACKER_TRIAGE](26_FILL_TRACKER_TRIAGE.md#fillcursornonmonotonicrejected) |

### Ticket / Info alerts

| Alert | Severity | Dashboard | What to check | Runbook |
|-------|----------|-----------|---------------|---------|
| `GrinderDataGapSpike` | ticket | Overview | Tick gap counters | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdatagapspike) |
| `GrinderDataOutlierSpike` | ticket | Overview | Outlier counters | [23_DATA_QUALITY_TRIAGE](23_DATA_QUALITY_TRIAGE.md#grinderdataoutlierspike) |
| `GrinderHttpReadRetriesSpike` | ticket | Trading Loop | Route+Method panels | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttpreadretriesspike) |
| `GrinderHttp429RateLimitSpike` | ticket | Trading Loop | Route+Method panels | [24_LATENCY_RETRY_TRIAGE](24_LATENCY_RETRY_TRIAGE.md#grinderhttp429ratelimitspike) |
| `KillSwitchTripIncreased` | info | Overview | Kill-switch trips | [04_KILL_SWITCH](04_KILL_SWITCH.md) |
| `GrinderRecentRestart` | info | Trading Loop | Uptime seconds | Verify intentional; check logs if not |
| `AllGatingBlocked` | info | Overview | Allowed/Blocked counters | All decisions blocked for 5m |
| `ReconcileRemediationPlanned` | info | Reconcile | Planned actions | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileremediationplanned) |
| `ReconcileRemediationBlocked` | info | Reconcile | Blocked actions | [16_RECONCILE_ALERTS_SLOS](16_RECONCILE_ALERTS_SLOS.md#reconcileremediationblocked) |
| `MlActiveModePersistentlyBlocked` | info | ML Overview | Active mode gauge | [18_ML_INFERENCE_SLOS](18_ML_INFERENCE_SLOS.md#mlactivemodepersistentlyblocked) |
| `SorNoopSpike` | info | Overview | Router decisions | [28_SOR_FIRE_DRILL](28_SOR_FIRE_DRILL.md#sornoopspike) |

---

## Using triage bundle when an alert fires

The CI/CD pipeline automatically generates triage bundles on failure, but you can
also run one manually when investigating an alert:

**Full triage (all sections):**
```bash
bash scripts/triage_bundle.sh 2>&1 | tee /tmp/triage_$(date +%s).txt
```

**Compact triage (metadata + readyz + next steps only):**
```bash
bash scripts/triage_bundle.sh --compact 2>&1
```

**Where to find CI triage artifacts:**
- PR comments: look for "Latest triage hints" block (compact preview)
- GitHub Actions artifacts: download full bundle from the workflow run
- 1-click link: the consolidated triage comment includes a direct artifact URL

**What to read first in the triage output:**
1. `NEXT STEPS` section -- prioritized actions based on current state
2. `READYZ` section -- is the loop ready?
3. `METRICS SNAPSHOT` section -- key counters at the time of capture

---

## Severity reference

| Severity | Meaning | Response time | Examples |
|----------|---------|---------------|----------|
| `critical` | Immediate action required | <5 min | GrinderDown, EngineInitDown, KillSwitchTripped |
| `page` | Wake-up worthy | <15 min | GrinderDataStaleBurst, FillCursorSaveErrors |
| `warning` | Investigate soon | <1 hour | ReadyzNotReady, HighDrawdown, ReconcileSnapshotStale |
| `ticket` | Track and fix | Next business day | GrinderDataGapSpike, GrinderHttp429RateLimitSpike |
| `info` | Informational | Review in batch | GrinderRecentRestart, SorNoopSpike |

---

## Alert label/annotation contract (OBS-3)

Every alert in `monitoring/alert_rules.yml` MUST carry these labels and annotations.
This contract is enforced by `scripts/verify_alert_rules.py` (OBS-4).

### Required labels

| Label | Enum values | Purpose |
|-------|-------------|---------|
| `severity` | `critical` / `page` / `warning` / `ticket` / `info` | Response time SLA (see table above) |
| `component` | `scrape` / `process` / `readyz` / `engine` / `risk` / `gating` / `reconcile` / `ml` / `dq` / `exchange` / `fills` / `fsm` / `sor` / `account` | Which subsystem is affected |
| `category` | `availability` / `safety` / `latency` / `correctness` / `capacity` / `integrity` | What kind of failure |

### Required annotations

| Annotation | Required for | Purpose |
|------------|-------------|---------|
| `summary` | all | One-line human description |
| `description` | all | Full context with `{{ $value }}` / `{{ $labels.X }}` |
| `runbook_url` | all | Relative path to runbook anchor |
| `dashboard_uid` | `critical` / `page` only | Grafana dashboard UID for quick navigation |

### SSOT

- Contract header: `monitoring/alert_rules.yml` lines 1-12
- Label values: `monitoring/alert_rules.yml` (grep `component:` / `category:`)
- Dashboard UIDs: `grinder-overview`, `grinder-trading-loop`, `grinder-reconcile`, `grinder-ml-overview`, `prometheus-targets`

---

## Alert silencing (maintenance)

During planned maintenance, silence alerts in Prometheus Alertmanager:

```bash
# Example: Silence all grinder alerts for 1 hour
curl -X POST http://localhost:9093/api/v2/silences -d '{
  "matchers": [{"name": "job", "value": "grinder"}],
  "startsAt": "2024-01-01T00:00:00Z",
  "endsAt": "2024-01-01T01:00:00Z",
  "createdBy": "operator",
  "comment": "Planned maintenance"
}'
```

**Note:** Alertmanager is not currently deployed. Silencing would need to be done
via Grafana or by modifying alert rules.

---

## Escalation

| Severity | Response time | Escalation |
|----------|---------------|------------|
| Critical | Immediate | Page on-call |
| Warning | 15 minutes | Slack notification |
| Info | Next business day | Email summary |
