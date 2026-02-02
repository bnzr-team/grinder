# Runbook: HA Operations

## Overview

This runbook covers High Availability (HA) operations for GRINDER deployments with Redis-based leader election.

---

## Components

| Component | Path | Purpose |
|-----------|------|---------|
| HA module | `src/grinder/ha/` | Leader election and role management |
| Leader elector | `src/grinder/ha/leader.py` | Redis lease-lock coordination |
| HA compose | `docker-compose.ha.yml` | 2-replica HA deployment |

---

## Starting the HA Stack

### 1. Start with Docker Compose

```bash
docker compose -f docker-compose.ha.yml up --build -d
```

### 2. Verify All Services Running

```bash
docker compose -f docker-compose.ha.yml ps
```

Expected output:
```
NAME               STATUS          PORTS
grinder-redis      Up (healthy)    6379/tcp
grinder_live_1     Up (healthy)    0.0.0.0:9090->9090/tcp
grinder_live_2     Up (healthy)    0.0.0.0:9091->9090/tcp
```

### 3. Check Which Instance is ACTIVE

```bash
# Check instance 1
curl -fsS http://localhost:9090/readyz
# Check instance 2
curl -fsS http://localhost:9091/readyz
```

Only one should return `{"ready": true, "role": "active"}`.
The other should return `{"ready": false, "role": "standby"}`.

---

## Failover Testing

### Test: Stop Active Instance

1. Identify the ACTIVE instance:
   ```bash
   curl -fsS http://localhost:9090/readyz
   curl -fsS http://localhost:9091/readyz
   ```

2. Stop the ACTIVE instance:
   ```bash
   docker stop grinder_live_1  # or grinder_live_2
   ```

3. Wait for failover (~10 seconds, lock TTL):
   ```bash
   sleep 12
   ```

4. Verify the other instance became ACTIVE:
   ```bash
   curl -fsS http://localhost:9091/readyz  # or 9090
   # Expected: {"ready": true, "role": "active"}
   ```

### Test: Redis Failure

1. Stop Redis:
   ```bash
   docker stop grinder-redis
   ```

2. Verify both instances become STANDBY:
   ```bash
   curl -fsS http://localhost:9090/readyz
   curl -fsS http://localhost:9091/readyz
   # Both should return: {"ready": false, "role": "standby"}
   ```

3. Restart Redis:
   ```bash
   docker start grinder-redis
   ```

4. Wait for lock acquisition (~3 seconds, renewal interval):
   ```bash
   sleep 5
   ```

5. Verify one instance became ACTIVE again:
   ```bash
   curl -fsS http://localhost:9090/readyz
   curl -fsS http://localhost:9091/readyz
   ```

---

## Monitoring HA Status

### Check HA Metrics

```bash
# Get HA role metric
curl -fsS http://localhost:9090/metrics | grep grinder_ha_role
curl -fsS http://localhost:9091/metrics | grep grinder_ha_role
```

Expected output:
- ACTIVE instance: `grinder_ha_role{role="active"} 1`
- STANDBY instance: `grinder_ha_role{role="standby"} 1`

### Grafana Dashboard

If using the observability stack, HA role is visible in the GRINDER Overview dashboard.

---

## Troubleshooting

### Both Instances are STANDBY

**Symptom:** Both `/readyz` return `{"ready": false, "role": "standby"}`

**Cause:** Redis is unavailable or unreachable.

**Action:**
1. Check Redis is running:
   ```bash
   docker ps | grep redis
   ```

2. Check Redis connectivity:
   ```bash
   docker exec grinder_live_1 python -c "import redis; r = redis.Redis.from_url('redis://redis:6379/0'); print(r.ping())"
   ```

3. Check Redis logs:
   ```bash
   docker logs grinder-redis --tail=50
   ```

### Flapping Between ACTIVE and STANDBY

**Symptom:** Instance keeps switching roles rapidly.

**Cause:** Network latency causing lock renewal failures.

**Action:**
1. Check instance logs for lock failures:
   ```bash
   docker logs grinder_live_1 --tail=100 | grep -i lock
   ```

2. Consider increasing lock TTL (requires config change):
   - Current: 10s TTL, 3s renewal
   - Increase to: 15s TTL, 5s renewal

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRINDER_REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `GRINDER_HA_ENABLED` | `false` | Enable HA mode |

### Tuning Parameters

In `src/grinder/ha/leader.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `lock_ttl_ms` | 10000 | Lock TTL in milliseconds |
| `renew_interval_ms` | 3000 | Lock renewal interval |
| `lock_key` | `grinder:leader:lock` | Redis key for leader lock |

---

## Stopping the HA Stack

```bash
docker compose -f docker-compose.ha.yml down -v
```

The `-v` flag removes volumes (including Redis data).
