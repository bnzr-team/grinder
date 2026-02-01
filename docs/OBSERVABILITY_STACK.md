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
