# GRINDER Runbooks

Operational runbooks for GRINDER trading system.

## Index

| Runbook | Description |
|---------|-------------|
| [01_STARTUP_SHUTDOWN](01_STARTUP_SHUTDOWN.md) | How to start and stop GRINDER |
| [02_HEALTH_TRIAGE](02_HEALTH_TRIAGE.md) | Quick health checks and diagnostics |
| [03_METRICS_DASHBOARDS](03_METRICS_DASHBOARDS.md) | Prometheus metrics and Grafana dashboards |
| [04_KILL_SWITCH](04_KILL_SWITCH.md) | Kill-switch events and recovery |
| [05_SOAK_GATE](05_SOAK_GATE.md) | Running soak tests and interpreting results |
| [06_ALERT_RESPONSE](06_ALERT_RESPONSE.md) | Responding to Prometheus alerts |

## Quick Reference

| Endpoint | URL | Purpose |
|----------|-----|---------|
| Health | `http://localhost:9090/healthz` | Liveness check |
| Metrics | `http://localhost:9090/metrics` | Prometheus scrape |
| Prometheus | `http://localhost:9091` | Metrics UI |
| Grafana | `http://localhost:3000` | Dashboards (admin/admin) |

## Prerequisites

- Docker and docker-compose installed
- Repository cloned with submodules
- `.env` file configured (copy from `.env.example`)
