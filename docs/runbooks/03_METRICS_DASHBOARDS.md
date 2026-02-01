# Runbook: Metrics & Dashboards

## Overview

How to access and interpret GRINDER metrics via Prometheus and Grafana.

---

## Endpoints

| Service | URL | Credentials |
|---------|-----|-------------|
| GRINDER Metrics | http://localhost:9090/metrics | None |
| Prometheus UI | http://localhost:9091 | None |
| Grafana | http://localhost:3000 | admin / admin |

---

## Check Raw Metrics

### 1. Fetch All Metrics

```bash
curl -fsS http://localhost:9090/metrics
```

### 2. Check Specific Metric Groups

**System metrics:**

```bash
curl -fsS http://localhost:9090/metrics | grep -E "^grinder_up|^grinder_uptime"
```

**Gating metrics:**

```bash
curl -fsS http://localhost:9090/metrics | grep "grinder_gating"
```

**Risk metrics:**

```bash
curl -fsS http://localhost:9090/metrics | grep -E "grinder_kill_switch|grinder_drawdown|grinder_high_water"
```

---

## Metrics Reference

### System Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `grinder_up` | gauge | 1 if running, 0 if down |
| `grinder_uptime_seconds` | gauge | Seconds since start |

### Gating Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_gating_allowed_total` | counter | `gate` | Allowed decisions by gate |
| `grinder_gating_blocked_total` | counter | `gate`, `reason` | Blocked decisions |

### Risk Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_kill_switch_triggered` | gauge | - | 1 if active, 0 otherwise |
| `grinder_kill_switch_trips_total` | counter | `reason` | Total trips by reason |
| `grinder_drawdown_pct` | gauge | - | Current drawdown (0-100) |
| `grinder_high_water_mark` | gauge | - | Equity high-water mark |

---

## Grafana Dashboards

### Access Dashboard

1. Open http://localhost:3000
2. Login: `admin` / `admin`
3. Navigate to: Dashboards > GRINDER Overview

### Dashboard Location

Dashboard JSON is provisioned from:

```
monitoring/grafana/provisioning/dashboards/grinder.json
```

### Dashboard Panels

| Panel | Metric | Purpose |
|-------|--------|---------|
| Status | `grinder_up` | UP/DOWN indicator |
| Uptime | `grinder_uptime_seconds` | Time since start |
| Allowed Total | `sum(grinder_gating_allowed_total)` | Total allowed decisions |
| Blocked Total | `sum(grinder_gating_blocked_total)` | Total blocked decisions |
| Kill Switch | `grinder_kill_switch_triggered` | OK/TRIPPED indicator |
| Drawdown % | `grinder_drawdown_pct` | Current drawdown |

---

## Prometheus Queries

### Useful PromQL Queries

**Rate of blocked decisions (per minute):**

```promql
sum(rate(grinder_gating_blocked_total[1m]))
```

**Blocked by reason:**

```promql
sum by (reason) (grinder_gating_blocked_total)
```

**Kill-switch trips in last hour:**

```promql
increase(grinder_kill_switch_trips_total[1h])
```

**Drawdown trend:**

```promql
grinder_drawdown_pct
```

---

## Troubleshooting

### Metrics Not Showing in Prometheus

1. Check target status: http://localhost:9091/targets
2. Verify grinder target is UP
3. If DOWN, check network connectivity between containers

### Dashboard Shows "No Data"

1. Verify time range is recent (last 15m)
2. Check Prometheus datasource in Grafana
3. Query metrics directly: http://localhost:9091/graph

### Metrics Stale

```bash
# Check last scrape time in Prometheus
curl -s http://localhost:9091/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job=="grinder") | .lastScrape'
```
