# Observability Stack v0

Local development monitoring stack with Prometheus and Grafana.

## Quick Start

```bash
# Start the stack (grinder + prometheus + grafana)
docker compose -f docker-compose.observability.yml up --build -d

# Check services are running
docker compose -f docker-compose.observability.yml ps

# Stop and cleanup
docker compose -f docker-compose.observability.yml down -v
```

## Smoke Test

Run the automated smoke test to verify the full stack is healthy:

```bash
bash scripts/docker_smoke_observability.sh
```

This script:
1. Starts all services (grinder, prometheus, grafana)
2. Waits for each service to be healthy
3. Verifies Prometheus is scraping grinder (`health: "up"`)
4. Cleans up automatically on exit

The smoke test is also run in CI on every PR that touches:
- `Dockerfile`
- `docker-compose.observability.yml`
- `monitoring/**`
- `src/**`
- `scripts/**`

## Ports

**Note:** Prometheus runs on host port 9091 (mapped from container port 9090) to avoid conflict with grinder's 9090.

| Service | Port | URL |
|---------|------|-----|
| GRINDER | 9090 | http://localhost:9090/metrics |
| Prometheus | 9091 | http://localhost:9091 |
| Grafana | 3000 | http://localhost:3000 |

## Health Checks

```bash
# GRINDER metrics endpoint
curl -s http://localhost:9090/metrics | head -20

# Prometheus ready
curl -s http://localhost:9091/-/ready

# Grafana health
curl -s http://localhost:3000/api/health
```

## Verify Prometheus Scraping

```bash
# Check targets status
curl -s "http://localhost:9091/api/v1/targets" | python3 -m json.tool | head -40

# Should show grinder target with health="up"
```

## Grafana Access

- **URL:** http://localhost:3000
- **Username:** admin
- **Password:** admin
- **Dashboard:** GRINDER Overview (auto-provisioned)

Anonymous read access is enabled by default.

## Available Metrics

From `src/grinder/observability/metrics_builder.py`:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_up` | gauge | - | 1 if running |
| `grinder_uptime_seconds` | gauge | - | Uptime in seconds |
| `grinder_gating_allowed_total` | counter | gate | Allowed gating decisions |
| `grinder_gating_blocked_total` | counter | gate, reason | Blocked gating decisions |

## Alert Rules

Defined in `monitoring/alert_rules.yml`:

| Alert | Severity | Condition |
|-------|----------|-----------|
| GrinderDown | critical | `grinder_up == 0` for 1m |
| GrinderTargetDown | critical | `up{job="grinder"} == 0` for 1m |
| HighGatingBlocks | warning | Block rate > 0.1/sec for 5m |
| ToxicityTriggers | warning | SPREAD_SPIKE or PRICE_IMPACT_HIGH blocks |
| AllGatingBlocked | info | No allowed, only blocked for 5m |
| GrinderRecentRestart | info | Uptime < 60s |

## Dashboard Panels

The GRINDER Overview dashboard includes:

1. **Status** — UP/DOWN indicator
2. **Uptime** — Current uptime
3. **Allowed Total** — Total allowed gating decisions
4. **Blocked Total** — Total blocked gating decisions
5. **Uptime Over Time** — Uptime graph
6. **Gating Decisions Rate** — Allowed vs blocked per second
7. **Blocked by Reason** — Table of blocks by gate and reason
8. **Allowed by Gate** — Table of allows by gate

## Launch-13/14/15 Quick Panels

Recommended PromQL queries for monitoring the P1 hardening pack subsystems.
These are not auto-provisioned in Grafana yet — use them in Prometheus UI
(`http://localhost:9091`) or paste into custom Grafana panels.

### FSM (Launch-13)

Metrics from `src/grinder/live/fsm_metrics.py`.

| Panel | PromQL | Type | Notes |
|-------|--------|------|-------|
| Current state | `grinder_fsm_current_state` | Gauge (per-state) | `{state="ACTIVE"}=1` means healthy; `{state="DEGRADED\|EMERGENCY\|PAUSED"}=1` needs attention |
| State duration | `grinder_fsm_state_duration_seconds` | Gauge | >120s in bad state fires `FsmBadStateTooLong` alert |
| Transitions | `sum by (from_state,to_state,reason) (increase(grinder_fsm_transitions_total[5m]))` | Counter | Spike = state churn; check reasons |
| Blocked intents | `sum(increase(grinder_fsm_action_blocked_total[5m]))` | Counter | >0 fires `FsmActionBlockedSpike` alert |

**Drilldowns:** Break by `state=` and `intent=` labels on `grinder_fsm_action_blocked_total`.

**What good looks like:** `grinder_fsm_current_state{state="ACTIVE"}=1`, duration stable, zero blocked intents.

**What bad looks like:** State stuck in DEGRADED >2min, blocked intents climbing.

**Next step:** [27_FSM_OPERATOR_OVERRIDE.md](runbooks/27_FSM_OPERATOR_OVERRIDE.md)

### SOR (Launch-14)

Metrics from `src/grinder/execution/sor_metrics.py`.

| Panel | PromQL | Type | Notes |
|-------|--------|------|-------|
| Decisions by type | `sum by (decision,reason) (increase(grinder_router_decision_total[5m]))` | Counter | Normal mix: PLACE + AMEND + CANCEL; watch BLOCK/NOOP ratio |
| Blocked orders | `increase(grinder_router_decision_total{decision="BLOCK"}[5m])` | Counter | >0 fires `SorBlockedSpike` alert; check `reason` label |
| NOOP orders | `increase(grinder_router_decision_total{decision="NOOP"}[5m])` | Counter | >0 fires `SorNoopSpike` (info); normal during low-activity |
| Amend savings | `grinder_router_amend_savings_total` | Counter | Higher = more order amendments saved vs cancel+place pairs |

**Drilldowns:** Break by `decision=` and `reason=` labels on `grinder_router_decision_total`.

**What good looks like:** Mix of PLACE/AMEND/CANCEL decisions, zero BLOCKs, low NOOPs.

**What bad looks like:** Sustained BLOCKs (router rejecting orders), or 100% NOOP (nothing to do).

**Next step:** `bash scripts/ops_fill_triage.sh sor-fire-drill` or [28_SOR_FIRE_DRILL.md](runbooks/28_SOR_FIRE_DRILL.md)

### AccountSync (Launch-15)

Metrics from `src/grinder/account/metrics.py`.

| Panel | PromQL | Type | Notes |
|-------|--------|------|-------|
| Sync age | `grinder_account_sync_age_seconds` | Gauge | >120s fires `AccountSyncStale` (if `last_ts > 0`) |
| Last sync timestamp | `grinder_account_sync_last_ts` | Gauge | 0 = never synced (feature disabled or just started) |
| Errors by reason | `sum by (reason) (increase(grinder_account_sync_errors_total[5m]))` | Counter | >0 fires `AccountSyncErrors`; check `reason` label |
| Mismatches by rule | `sum by (rule) (increase(grinder_account_sync_mismatches_total[5m]))` | Counter | >0 fires `AccountSyncMismatchSpike` |
| Position count | `grinder_account_sync_positions_count` | Gauge | Sanity: expected number of open positions |
| Open orders count | `grinder_account_sync_open_orders_count` | Gauge | Sanity: expected number of open orders |
| Pending notional | `grinder_account_sync_pending_notional` | Gauge | Total notional of open orders; watch for unexpected jumps |

**Drilldowns:** Break by `reason=` on errors, `rule=` on mismatches.

**What good looks like:** `age_seconds < 30`, zero errors, zero mismatches, position/order counts match expectations.

**What bad looks like:** `age_seconds` climbing (stale sync), errors by `http`/`auth`/`parse` reasons, mismatch spike.

**Next step:** `bash scripts/ops_fill_triage.sh account-sync-drill` or [29_ACCOUNT_SYNC.md](runbooks/29_ACCOUNT_SYNC.md)

### Consecutive Loss Guard (PR-C3b)

Metrics from `src/grinder/risk/consecutive_loss_wiring.py`.

| Panel | PromQL | Type | Notes |
|-------|--------|------|-------|
| Consecutive losses | `grinder_risk_consecutive_losses` | Gauge | Current streak count; resets on win/breakeven |
| Guard trips | `increase(grinder_risk_consecutive_loss_trips_total[5m])` | Counter | >0 fires `ConsecutiveLossTrip` alert (critical) |

**What good looks like:** `consecutive_losses` < 3, zero trips.

**What bad looks like:** `consecutive_losses` climbing toward threshold, trip count increasing.

**Scope:** Wired only in `scripts/run_live_reconcile.py`. Other entrypoints do not activate this guard.

**Next step:** See [02_HEALTH_TRIAGE.md -- Consecutive Loss Guard](runbooks/02_HEALTH_TRIAGE.md#consecutive-loss-guard-pr-c3b)

## File Structure

```
monitoring/
├── prometheus.yml          # Prometheus config (scrapes grinder:9090)
├── alert_rules.yml         # Prometheus alert rules
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── datasource.yml   # Auto-add Prometheus datasource
        └── dashboards/
            ├── dashboard.yml    # Dashboard provisioning config
            └── grinder.json     # GRINDER Overview dashboard
```

## Troubleshooting

### Prometheus shows target as "down"

1. Check grinder container is running:
   ```bash
   docker compose -f docker-compose.observability.yml ps grinder
   ```

2. Check grinder logs:
   ```bash
   docker compose -f docker-compose.observability.yml logs grinder
   ```

3. Verify metrics endpoint inside container:
   ```bash
   docker compose -f docker-compose.observability.yml exec grinder \
     python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9090/metrics').read().decode()[:500])"
   ```

### Grafana dashboard is empty

1. Check Prometheus datasource:
   - Go to http://localhost:3000/connections/datasources
   - Verify Prometheus is listed and "Test" succeeds

2. Check Prometheus has data:
   ```bash
   curl -s "http://localhost:9091/api/v1/query?query=grinder_up"
   ```

### Container won't start

1. Check for port conflicts:
   ```bash
   lsof -i :9090 -i :9091 -i :3000
   ```

2. Remove old volumes and rebuild:
   ```bash
   docker compose -f docker-compose.observability.yml down -v
   docker compose -f docker-compose.observability.yml up --build -d
   ```

## Non-Goals (v0)

- No Kubernetes manifests
- No Helm charts
- No real alert delivery (Slack, email, PagerDuty)
- No long-term storage or federation
