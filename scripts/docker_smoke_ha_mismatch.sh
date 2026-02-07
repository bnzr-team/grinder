#!/bin/bash
# LC-20: HA Leader-Only Remediation Smoke Test
#
# This script verifies that only the HA leader can execute remediation,
# while followers are BLOCKED with reason="not_leader".
#
# Test flow:
# 1. Start HA stack (2 grinder instances + redis)
# 2. Wait for leader election
# 3. Inject a fake mismatch and attempt remediation on EACH instance
# 4. Verify:
#    - Leader: action_planned_total{action="cancel_all"} >= 1 (dry_run mode)
#    - Follower: action_blocked_total{reason="not_leader"} >= 1
#    - Follower: action_executed_total{action="cancel_all"} == 0
#
# Usage:
#   ./scripts/docker_smoke_ha_mismatch.sh
#
# Exit codes:
#   0 - All assertions pass
#   1 - Test failure

set -euo pipefail

# Detect docker compose command (v2 plugin or v1 standalone)
if docker compose version &>/dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    echo "ERROR: Neither 'docker compose' nor 'docker-compose' found"
    exit 1
fi

echo "============================================================"
echo "  LC-20: HA Leader-Only Remediation Smoke Test"
echo "============================================================"
echo ""

# Cleanup on exit
cleanup() {
    echo ""
    echo "=== Cleanup ==="
    $COMPOSE -f docker-compose.ha.yml down -v 2>/dev/null || true
}
trap cleanup EXIT

# Start HA stack
echo "=== Starting HA stack ==="
$COMPOSE -f docker-compose.ha.yml up --build -d
echo "Waiting for containers to be healthy..."
sleep 15

# Verify all 3 services are running
echo ""
echo "=== Verifying services ==="
$COMPOSE -f docker-compose.ha.yml ps
# Count running containers (compatible with both docker-compose v1 and docker compose v2)
RUNNING_COUNT=$(docker ps --filter "name=grinder" --filter "status=running" -q | wc -l)
if [[ "$RUNNING_COUNT" -ne 3 ]]; then
    echo "ERROR: Expected 3 running services, got $RUNNING_COUNT"
    exit 1
fi
echo "All 3 services running"

# Wait a bit more for leader election to stabilize
sleep 5

# Determine leader and follower
echo ""
echo "=== Checking leader election ==="
IS_LEADER_1=$(curl -fsS http://localhost:9090/metrics | grep "^grinder_ha_is_leader " | awk '{print $2}')
IS_LEADER_2=$(curl -fsS http://localhost:9091/metrics | grep "^grinder_ha_is_leader " | awk '{print $2}')

echo "Instance 1 is_leader: $IS_LEADER_1"
echo "Instance 2 is_leader: $IS_LEADER_2"

if [[ "$IS_LEADER_1" == "1" && "$IS_LEADER_2" == "0" ]]; then
    LEADER_CONTAINER="grinder_live_1"
    FOLLOWER_CONTAINER="grinder_live_2"
    LEADER_PORT=9090
    FOLLOWER_PORT=9091
    echo "Instance 1 is LEADER, Instance 2 is FOLLOWER"
elif [[ "$IS_LEADER_1" == "0" && "$IS_LEADER_2" == "1" ]]; then
    LEADER_CONTAINER="grinder_live_2"
    FOLLOWER_CONTAINER="grinder_live_1"
    LEADER_PORT=9091
    FOLLOWER_PORT=9090
    echo "Instance 2 is LEADER, Instance 1 is FOLLOWER"
else
    echo "ERROR: Invalid leader state (expected exactly one leader)"
    exit 1
fi

# Copy the inject script to containers
echo ""
echo "=== Copying inject script to containers ==="
docker cp scripts/inject_mismatch_and_remediate.py grinder_live_1:/tmp/inject.py
docker cp scripts/inject_mismatch_and_remediate.py grinder_live_2:/tmp/inject.py

# Run inject script on LEADER (with role=active)
echo ""
echo "=== Running inject script on LEADER ($LEADER_CONTAINER) ==="
LEADER_RESULT=$(docker exec "$LEADER_CONTAINER" python /tmp/inject.py --role active)
echo "Leader result: $LEADER_RESULT"

# Parse leader result
LEADER_STATUS=$(echo "$LEADER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
LEADER_PLANNED=$(echo "$LEADER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['metrics']['action_planned_cancel_all'])")
LEADER_BLOCKED=$(echo "$LEADER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['metrics']['action_blocked_not_leader'])")

echo "  Status: $LEADER_STATUS"
echo "  Planned: $LEADER_PLANNED"
echo "  Blocked (not_leader): $LEADER_BLOCKED"

# Run inject script on FOLLOWER (with role=standby)
echo ""
echo "=== Running inject script on FOLLOWER ($FOLLOWER_CONTAINER) ==="
FOLLOWER_RESULT=$(docker exec "$FOLLOWER_CONTAINER" python /tmp/inject.py --role standby)
echo "Follower result: $FOLLOWER_RESULT"

# Parse follower result
FOLLOWER_STATUS=$(echo "$FOLLOWER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
FOLLOWER_BLOCK_REASON=$(echo "$FOLLOWER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['block_reason'] or '')")
FOLLOWER_PLANNED=$(echo "$FOLLOWER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['metrics']['action_planned_cancel_all'])")
FOLLOWER_BLOCKED=$(echo "$FOLLOWER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['metrics']['action_blocked_not_leader'])")
FOLLOWER_EXECUTED=$(echo "$FOLLOWER_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['metrics']['action_executed_cancel_all'])")

echo "  Status: $FOLLOWER_STATUS"
echo "  Block reason: $FOLLOWER_BLOCK_REASON"
echo "  Planned: $FOLLOWER_PLANNED"
echo "  Blocked (not_leader): $FOLLOWER_BLOCKED"
echo "  Executed: $FOLLOWER_EXECUTED"

# Fetch raw metrics for evidence
echo ""
echo "=== Raw metrics from both instances ==="
echo ""
echo "--- LEADER ($LEADER_CONTAINER:$LEADER_PORT) ---"
curl -fsS "http://localhost:$LEADER_PORT/metrics" | grep -E "grinder_reconcile_action_(planned|executed|blocked)_total" || echo "(no remediation metrics yet)"

echo ""
echo "--- FOLLOWER ($FOLLOWER_CONTAINER:$FOLLOWER_PORT) ---"
curl -fsS "http://localhost:$FOLLOWER_PORT/metrics" | grep -E "grinder_reconcile_action_(planned|executed|blocked)_total" || echo "(no remediation metrics yet)"

# Assertions
echo ""
echo "============================================================"
echo "  ASSERTIONS"
echo "============================================================"

PASS=true

# 1. Leader should have status=planned (dry_run mode)
if [[ "$LEADER_STATUS" == "planned" ]]; then
    echo "PASS: Leader status is 'planned' (dry_run mode)"
else
    echo "FAIL: Leader status is '$LEADER_STATUS', expected 'planned'"
    PASS=false
fi

# 2. Leader should have planned_count >= 1
if [[ "$LEADER_PLANNED" -ge 1 ]]; then
    echo "PASS: Leader action_planned_total{action='cancel_all'} = $LEADER_PLANNED (>= 1)"
else
    echo "FAIL: Leader action_planned_total{action='cancel_all'} = $LEADER_PLANNED (expected >= 1)"
    PASS=false
fi

# 3. Follower should have status=blocked
if [[ "$FOLLOWER_STATUS" == "blocked" ]]; then
    echo "PASS: Follower status is 'blocked'"
else
    echo "FAIL: Follower status is '$FOLLOWER_STATUS', expected 'blocked'"
    PASS=false
fi

# 4. Follower block_reason should be not_leader
if [[ "$FOLLOWER_BLOCK_REASON" == "not_leader" ]]; then
    echo "PASS: Follower block_reason is 'not_leader'"
else
    echo "FAIL: Follower block_reason is '$FOLLOWER_BLOCK_REASON', expected 'not_leader'"
    PASS=false
fi

# 5. Follower should have blocked_count >= 1 for not_leader
if [[ "$FOLLOWER_BLOCKED" -ge 1 ]]; then
    echo "PASS: Follower action_blocked_total{reason='not_leader'} = $FOLLOWER_BLOCKED (>= 1)"
else
    echo "FAIL: Follower action_blocked_total{reason='not_leader'} = $FOLLOWER_BLOCKED (expected >= 1)"
    PASS=false
fi

# 6. Follower should have executed_count == 0
if [[ "$FOLLOWER_EXECUTED" -eq 0 ]]; then
    echo "PASS: Follower action_executed_total{action='cancel_all'} = $FOLLOWER_EXECUTED (== 0)"
else
    echo "FAIL: Follower action_executed_total{action='cancel_all'} = $FOLLOWER_EXECUTED (expected 0)"
    PASS=false
fi

# 7. Follower should have planned_count == 0 (NOT_LEADER is not a planning reason)
if [[ "$FOLLOWER_PLANNED" -eq 0 ]]; then
    echo "PASS: Follower action_planned_total{action='cancel_all'} = $FOLLOWER_PLANNED (== 0)"
else
    echo "FAIL: Follower action_planned_total{action='cancel_all'} = $FOLLOWER_PLANNED (expected 0)"
    PASS=false
fi

echo ""
echo "============================================================"
if [[ "$PASS" == "true" ]]; then
    echo "  ALL ASSERTIONS PASSED"
    echo "============================================================"
    exit 0
else
    echo "  SOME ASSERTIONS FAILED"
    echo "============================================================"
    exit 1
fi
# LC-20 HA Smoke Test
