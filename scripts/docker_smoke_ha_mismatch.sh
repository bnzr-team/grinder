#!/bin/bash
# LC-20: HA Leader-Only Remediation Smoke Test
#
# This script verifies that only the HA leader can execute remediation,
# while followers are BLOCKED with reason="not_leader".
#
# Test flow:
# 1. Start HA stack (2 grinder instances + redis)
# 2. Wait for services to be ready (bounded retries)
# 3. Wait for stable leader election (bounded retries)
# 4. Inject a fake mismatch and attempt remediation on EACH instance
# 5. Verify:
#    - Leader: action_planned_total{action="cancel_all"} >= 1 (dry_run mode)
#    - Follower: action_blocked_total{reason="not_leader"} >= 1
#    - Follower: action_executed_total{action="cancel_all"} == 0
#
# Usage:
#   ./scripts/docker_smoke_ha_mismatch.sh
#
# Environment variables:
#   SMOKE_FORCE_FAIL=1  - Force failure after stack starts (for testing diagnostics)
#
# Exit codes:
#   0 - All assertions pass
#   1 - Assertion failure
#   2 - Infrastructure/timeout failure

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
READINESS_TIMEOUT_S=60
LEADER_ELECTION_TIMEOUT_S=60
RETRY_INTERVAL_S=2
LEADER_STABLE_COUNT=2  # Require stable leader state N times in a row

# =============================================================================
# Global state for diagnostics
# =============================================================================
EXIT_CODE=0
DIAGNOSTICS_DUMPED=false

# =============================================================================
# Helper functions
# =============================================================================

# Detect docker compose command (v2 plugin or v1 standalone)
detect_compose() {
    if docker compose version &>/dev/null; then
        COMPOSE="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE="docker-compose"
    else
        echo "ERROR: Neither 'docker compose' nor 'docker-compose' found"
        exit 2
    fi
}

# Retry a command until success or timeout
# Usage: retry_until "description" timeout_s interval_s command [args...]
retry_until() {
    local description="$1"
    local timeout_s="$2"
    local interval_s="$3"
    shift 3

    local start_time
    start_time=$(date +%s)
    local end_time=$((start_time + timeout_s))

    echo "  Waiting for: $description (timeout: ${timeout_s}s)"

    while true; do
        if "$@" &>/dev/null; then
            echo "  OK: $description"
            return 0
        fi

        local now
        now=$(date +%s)
        if [[ "$now" -ge "$end_time" ]]; then
            echo "  TIMEOUT: $description (after ${timeout_s}s)"
            return 1
        fi

        sleep "$interval_s"
    done
}

# Check if a service endpoint is reachable
check_endpoint() {
    local url="$1"
    curl -fsS --max-time 5 "$url" >/dev/null 2>&1
}

# Check if metrics endpoint returns grinder_ha_is_leader
check_metrics_has_leader() {
    local port="$1"
    curl -fsS --max-time 5 "http://localhost:$port/metrics" 2>/dev/null | grep -q "^grinder_ha_is_leader "
}

# Get leader state from metrics (returns 0 or 1, or empty on error)
get_leader_state() {
    local port="$1"
    curl -fsS --max-time 5 "http://localhost:$port/metrics" 2>/dev/null | grep "^grinder_ha_is_leader " | awk '{print $2}' || echo ""
}

# Check if exactly one leader exists and state is stable
# Returns 0 if valid (1,0) or (0,1), 1 otherwise
check_leader_election_valid() {
    local state1 state2
    state1=$(get_leader_state 9090)
    state2=$(get_leader_state 9091)

    if [[ "$state1" == "1" && "$state2" == "0" ]]; then
        return 0
    elif [[ "$state1" == "0" && "$state2" == "1" ]]; then
        return 0
    else
        return 1
    fi
}

# Dump diagnostics on failure
dump_diagnostics() {
    # Only dump once and only on failure
    if [[ "$DIAGNOSTICS_DUMPED" == "true" ]]; then
        return
    fi
    if [[ "$EXIT_CODE" -eq 0 ]]; then
        return
    fi
    DIAGNOSTICS_DUMPED=true

    echo ""
    echo "============================================================"
    echo "  DIAGNOSTICS (exit_code=$EXIT_CODE)"
    echo "============================================================"

    echo ""
    echo "--- docker compose ps ---"
    $COMPOSE -f docker-compose.ha.yml ps 2>/dev/null || echo "(unavailable)"

    echo ""
    echo "--- docker ps (grinder containers) ---"
    docker ps --filter "name=grinder" 2>/dev/null || echo "(unavailable)"

    echo ""
    echo "--- Container logs (last 100 lines each) ---"
    echo ""
    echo "[grinder_live_1]"
    $COMPOSE -f docker-compose.ha.yml logs --no-color --tail=100 grinder_live_1 2>/dev/null || echo "(unavailable)"
    echo ""
    echo "[grinder_live_2]"
    $COMPOSE -f docker-compose.ha.yml logs --no-color --tail=100 grinder_live_2 2>/dev/null || echo "(unavailable)"

    echo ""
    echo "--- HA metrics (port 9090) ---"
    curl -s --max-time 5 "http://localhost:9090/metrics" 2>/dev/null | grep -E "grinder_ha_is_leader|grinder_ha_role" || echo "(metrics unreachable)"

    echo ""
    echo "--- HA metrics (port 9091) ---"
    curl -s --max-time 5 "http://localhost:9091/metrics" 2>/dev/null | grep -E "grinder_ha_is_leader|grinder_ha_role" || echo "(metrics unreachable)"

    echo ""
    echo "============================================================"
}

# Cleanup containers
cleanup() {
    echo ""
    echo "=== Cleanup ==="
    $COMPOSE -f docker-compose.ha.yml down -v 2>/dev/null || true
}

# Combined exit handler
on_exit() {
    dump_diagnostics
    cleanup
}

# =============================================================================
# Main script
# =============================================================================

detect_compose
trap on_exit EXIT

echo "============================================================"
echo "  LC-20: HA Leader-Only Remediation Smoke Test"
echo "============================================================"
echo ""

# Start HA stack
echo "=== Starting HA stack ==="
$COMPOSE -f docker-compose.ha.yml up --build -d

# Wait for services to be ready (bounded)
echo ""
echo "=== Waiting for services to be ready ==="

if ! retry_until "grinder_live_1 metrics endpoint" "$READINESS_TIMEOUT_S" "$RETRY_INTERVAL_S" check_metrics_has_leader 9090; then
    echo "ERROR: grinder_live_1 did not become ready"
    EXIT_CODE=2
    exit 2
fi

if ! retry_until "grinder_live_2 metrics endpoint" "$READINESS_TIMEOUT_S" "$RETRY_INTERVAL_S" check_metrics_has_leader 9091; then
    echo "ERROR: grinder_live_2 did not become ready"
    EXIT_CODE=2
    exit 2
fi

# Verify all 3 services are running
echo ""
echo "=== Verifying services ==="
$COMPOSE -f docker-compose.ha.yml ps
RUNNING_COUNT=$(docker ps --filter "name=grinder" --filter "status=running" -q | wc -l)
if [[ "$RUNNING_COUNT" -ne 3 ]]; then
    echo "ERROR: Expected 3 running services, got $RUNNING_COUNT"
    EXIT_CODE=2
    exit 2
fi
echo "All 3 services running"

# Debug hook: force failure to test diagnostics
if [[ "${SMOKE_FORCE_FAIL:-}" == "1" ]]; then
    echo "SMOKE_FORCE_FAIL=1: forcing failure to test diagnostics"
    EXIT_CODE=2
    exit 2
fi

# Wait for stable leader election (bounded, require N stable readings)
echo ""
echo "=== Waiting for stable leader election ==="

STABLE_COUNT=0
START_TIME=$(date +%s)
END_TIME=$((START_TIME + LEADER_ELECTION_TIMEOUT_S))
LAST_STATE=""

while true; do
    STATE1=$(get_leader_state 9090)
    STATE2=$(get_leader_state 9091)
    CURRENT_STATE="${STATE1},${STATE2}"

    if check_leader_election_valid; then
        if [[ "$CURRENT_STATE" == "$LAST_STATE" ]]; then
            STABLE_COUNT=$((STABLE_COUNT + 1))
            echo "  Leader state ($CURRENT_STATE) stable: $STABLE_COUNT/$LEADER_STABLE_COUNT"
            if [[ "$STABLE_COUNT" -ge "$LEADER_STABLE_COUNT" ]]; then
                echo "  OK: Leader election stable"
                break
            fi
        else
            STABLE_COUNT=1
            echo "  Leader state changed to ($CURRENT_STATE), resetting stability counter"
        fi
        LAST_STATE="$CURRENT_STATE"
    else
        STABLE_COUNT=0
        LAST_STATE=""
        echo "  Waiting for valid leader state (current: $CURRENT_STATE)"
    fi

    NOW=$(date +%s)
    if [[ "$NOW" -ge "$END_TIME" ]]; then
        echo "ERROR: Leader election did not stabilize within ${LEADER_ELECTION_TIMEOUT_S}s"
        EXIT_CODE=2
        exit 2
    fi

    sleep "$RETRY_INTERVAL_S"
done

# Determine leader and follower
echo ""
echo "=== Checking leader election ==="
IS_LEADER_1=$(get_leader_state 9090)
IS_LEADER_2=$(get_leader_state 9091)

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
    EXIT_CODE=2
    exit 2
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
    EXIT_CODE=0
    exit 0
else
    echo "  SOME ASSERTIONS FAILED"
    echo "============================================================"
    EXIT_CODE=1
    exit 1
fi
