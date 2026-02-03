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

## Rolling Restart (Zero-Downtime HTTP)

Use this procedure to restart both instances without losing HTTP availability.

**Important:** Trading continuity depends on execution state. This procedure ensures `/healthz` and `/readyz` remain available throughout. Actual trading operations may pause briefly during role transitions.

### Procedure

1. **Identify current roles:**
   ```bash
   curl -fsS http://localhost:9090/readyz  # Instance 1
   curl -fsS http://localhost:9091/readyz  # Instance 2
   ```
   Note which is ACTIVE and which is STANDBY.

2. **Restart STANDBY first:**
   ```bash
   # If instance 2 is STANDBY:
   docker compose -f docker-compose.ha.yml restart grinder_live_2
   ```

3. **Wait for STANDBY to be healthy:**
   ```bash
   sleep 10
   curl -fsS http://localhost:9091/readyz
   # Should return: {"ready": false, "role": "standby"}
   ```

4. **Restart ACTIVE (triggers failover):**
   ```bash
   # If instance 1 is ACTIVE:
   docker compose -f docker-compose.ha.yml restart grinder_live_1
   ```

5. **Wait for failover (~10s TTL):**
   ```bash
   sleep 12
   ```

6. **Verify new ACTIVE:**
   ```bash
   curl -fsS http://localhost:9090/readyz
   curl -fsS http://localhost:9091/readyz
   # One should be active, one standby
   ```

7. **Verify old ACTIVE is now STANDBY:**
   ```bash
   # Instance 1 should now show standby
   curl -fsS http://localhost:9090/readyz
   # Expected: {"ready": false, "role": "standby"}
   ```

### Verification

After rolling restart:
- Both instances healthy: `docker compose -f docker-compose.ha.yml ps`
- Exactly one ACTIVE: check both `/readyz` endpoints
- Metrics flowing: `curl -fsS http://localhost:9090/metrics | grep grinder_up`

---

## One Node Down

### Scenario: Single Instance Failure

**Symptom:** One container is stopped/unhealthy, the other is running.

**Expected behavior:**
- If ACTIVE instance died → STANDBY becomes ACTIVE within ~10s (TTL)
- If STANDBY instance died → ACTIVE continues operating, no impact
- `/readyz` returns 200 on surviving ACTIVE instance

**Triage steps:**

1. **Check container status:**
   ```bash
   docker compose -f docker-compose.ha.yml ps
   ```

2. **Check which instance is ACTIVE:**
   ```bash
   curl -fsS http://localhost:9090/readyz 2>/dev/null || echo "Instance 1 unreachable"
   curl -fsS http://localhost:9091/readyz 2>/dev/null || echo "Instance 2 unreachable"
   ```

3. **Check HA metrics on surviving instance:**
   ```bash
   curl -fsS http://localhost:9090/metrics | grep grinder_ha_role
   ```

4. **Restart failed instance:**
   ```bash
   docker compose -f docker-compose.ha.yml restart grinder_live_1  # or _2
   ```

5. **Verify it comes back as STANDBY:**
   ```bash
   sleep 10
   curl -fsS http://localhost:9090/readyz
   # Should return standby (existing ACTIVE keeps the lock)
   ```

### Alerts to Watch

| Metric | Threshold | Meaning |
|--------|-----------|---------|
| Container health | unhealthy | Instance failed healthcheck |
| `grinder_ha_role{role="active"}` | 0 on both | Both standby = Redis issue |

---

## Redis Down / Redis Flapping

### Scenario: Redis Unavailable

**Symptom:** Both instances return `{"ready": false, "role": "standby"}` from `/readyz`.

**This is fail-safe behavior:** Without Redis, no instance can acquire the lock, so all become STANDBY. This prevents split-brain but stops trading.

**Triage steps:**

1. **Verify Redis is the cause:**
   ```bash
   docker ps | grep redis
   docker logs grinder-redis --tail=20
   ```

2. **Check Redis connectivity from instances:**
   ```bash
   docker exec grinder_live_1 python -c "
   import redis
   try:
       r = redis.Redis.from_url('redis://redis:6379/0')
       print('PING:', r.ping())
   except Exception as e:
       print('ERROR:', e)
   "
   ```

3. **Check instance logs for lock failures:**
   ```bash
   docker logs grinder_live_1 --tail=50 | grep -i "redis\|lock\|standby"
   ```

### Recovery

1. **Restart Redis:**
   ```bash
   docker compose -f docker-compose.ha.yml restart redis
   ```

2. **Wait for lock acquisition:**
   ```bash
   sleep 5  # renewal interval is 3s
   ```

3. **Verify one instance became ACTIVE:**
   ```bash
   curl -fsS http://localhost:9090/readyz
   curl -fsS http://localhost:9091/readyz
   ```

### Scenario: Redis Flapping (Intermittent Connectivity)

**Symptom:** Instances keep switching roles rapidly. Logs show repeated "Lost lock" / "Became ACTIVE" messages.

**Cause:** Network latency, Redis overload, or unstable connection.

**Triage:**

1. **Check Redis latency:**
   ```bash
   docker exec grinder-redis redis-cli --latency
   ```

2. **Check for lock churn in logs:**
   ```bash
   docker logs grinder_live_1 --tail=200 | grep -c "Became ACTIVE"
   # More than 1-2 per minute = flapping
   ```

**Mitigation:**

1. **Increase lock TTL** (requires restart):
   ```yaml
   # In docker-compose.ha.yml, add to environment:
   GRINDER_HA_LOCK_TTL_MS: "15000"      # 15s instead of 10s
   GRINDER_HA_RENEW_INTERVAL_MS: "5000" # 5s instead of 3s
   ```

2. **Check network between containers:**
   ```bash
   docker network inspect grinder-ha_default
   ```

---

## Stopping the HA Stack

```bash
docker compose -f docker-compose.ha.yml down -v
```

The `-v` flag removes volumes (including Redis data).
