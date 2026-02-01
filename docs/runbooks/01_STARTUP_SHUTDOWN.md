# Runbook: Startup / Shutdown

## Overview

How to start and stop GRINDER locally using Docker Compose.

## Prerequisites

- Docker installed and running
- Repository cloned
- `.env` file configured (copy from `.env.example` if needed)

---

## Startup

### 1. Start Full Stack (Grinder + Prometheus + Grafana)

```bash
docker compose -f docker-compose.observability.yml up --build -d
```

### 2. Verify Services Are Running

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

**What good looks like:**

```
NAMES               STATUS                   PORTS
grinder             Up X seconds (healthy)   0.0.0.0:9090->9090/tcp
grinder-prometheus  Up X seconds (healthy)   0.0.0.0:9091->9090/tcp
grinder-grafana     Up X seconds (healthy)   0.0.0.0:3000->3000/tcp
```

### 3. Verify Health Endpoint

```bash
curl -fsS http://localhost:9090/healthz
```

**What good looks like:**

```json
{"status": "ok", "uptime_s": 12.34}
```

---

## Shutdown

### 1. Graceful Shutdown (Preserves Volumes)

```bash
docker compose -f docker-compose.observability.yml down
```

### 2. Full Cleanup (Removes Volumes)

```bash
docker compose -f docker-compose.observability.yml down -v
```

**Use `-v` when:**
- You want to reset Prometheus/Grafana data
- Debugging data corruption issues
- Starting fresh for testing

---

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker compose -f docker-compose.observability.yml logs grinder

# Check if port is in use
lsof -i :9090
```

### Health Check Failing

```bash
# Check container health
docker inspect grinder --format='{{.State.Health.Status}}'

# Check recent health check output
docker inspect grinder --format='{{json .State.Health.Log}}' | jq '.[-1]'
```

### Container Keeps Restarting

```bash
# Check exit code
docker inspect grinder --format='{{.State.ExitCode}}'

# Check full logs
docker compose -f docker-compose.observability.yml logs --tail=100 grinder
```
