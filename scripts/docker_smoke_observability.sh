#!/usr/bin/env bash
# GRINDER Observability Stack Smoke Test
#
# Validates the full observability stack (grinder + prometheus + grafana)
# is healthy and properly wired.
#
# Usage:
#   ./scripts/docker_smoke_observability.sh
#
# Exit codes:
#   0 - All checks passed
#   1 - One or more checks failed

set -euo pipefail

COMPOSE_FILE="docker-compose.observability.yml"
MAX_WAIT_SECONDS=90
POLL_INTERVAL=5

# Detect docker compose command (v2 plugin or v1 standalone)
if docker compose version &>/dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "ERROR: Neither 'docker compose' nor 'docker-compose' found"
    exit 1
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Cleanup function - always runs on exit
cleanup() {
    local exit_code=$?
    log_info "Cleaning up..."

    if [ $exit_code -ne 0 ]; then
        log_error "Smoke test failed. Dumping diagnostics..."
        echo ""
        echo "=== Container Status ==="
        $COMPOSE_CMD -f "$COMPOSE_FILE" ps 2>/dev/null || true
        echo ""
        echo "=== Container Logs (last 100 lines) ==="
        $COMPOSE_CMD -f "$COMPOSE_FILE" logs --no-color --tail=100 2>/dev/null || true
    fi

    $COMPOSE_CMD -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    log_info "Cleanup complete."
}

trap cleanup EXIT

# Wait for endpoint to be ready
wait_for_endpoint() {
    local url="$1"
    local description="$2"
    local check_content="${3:-}"

    local elapsed=0
    while [ $elapsed -lt $MAX_WAIT_SECONDS ]; do
        if response=$(curl -sf "$url" 2>/dev/null); then
            if [ -n "$check_content" ]; then
                if echo "$response" | grep -q "$check_content"; then
                    log_info "$description: OK (found '$check_content')"
                    return 0
                fi
            else
                log_info "$description: OK"
                return 0
            fi
        fi
        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
        echo -n "."
    done
    echo ""
    log_error "$description: FAILED after ${MAX_WAIT_SECONDS}s"
    return 1
}

# Check Prometheus target health
check_prometheus_target() {
    local elapsed=0
    while [ $elapsed -lt $MAX_WAIT_SECONDS ]; do
        response=$(curl -sf "http://localhost:9091/api/v1/targets" 2>/dev/null) || true
        if echo "$response" | grep -q '"health":"up"'; then
            log_info "Prometheus target grinder: health=up"
            return 0
        fi
        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
        echo -n "."
    done
    echo ""
    log_error "Prometheus target not healthy after ${MAX_WAIT_SECONDS}s"
    # Show actual target status
    echo "Target response:"
    curl -sf "http://localhost:9091/api/v1/targets" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -50 || true
    return 1
}

main() {
    log_info "=== GRINDER Observability Stack Smoke Test ==="
    log_info "Compose file: $COMPOSE_FILE"
    log_info "Max wait: ${MAX_WAIT_SECONDS}s"
    echo ""

    # Start the stack
    log_info "Starting observability stack..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" up --build -d
    echo ""

    # Wait for services to be ready
    log_info "Waiting for services..."
    echo ""

    # Check 1: GRINDER /healthz
    log_info "Checking GRINDER /healthz..."
    wait_for_endpoint "http://localhost:9090/healthz" "GRINDER /healthz"

    # Check 2: GRINDER /metrics contains grinder_up
    log_info "Checking GRINDER /metrics..."
    wait_for_endpoint "http://localhost:9090/metrics" "GRINDER /metrics (grinder_up)" "grinder_up"

    # Check 3: Prometheus ready
    log_info "Checking Prometheus ready..."
    wait_for_endpoint "http://localhost:9091/-/ready" "Prometheus /-/ready"

    # Check 4: Prometheus scraping grinder successfully
    log_info "Checking Prometheus target health..."
    check_prometheus_target

    # Check 5: Grafana health (just verify 200 response - Docker healthcheck already validates)
    log_info "Checking Grafana health..."
    wait_for_endpoint "http://localhost:3000/api/health" "Grafana /api/health"

    echo ""
    log_info "=== All checks passed ==="
    echo ""

    # Print summary
    echo "Service Status:"
    $COMPOSE_CMD -f "$COMPOSE_FILE" ps
    echo ""

    # Print sample metrics
    echo "Sample metrics from GRINDER:"
    curl -sf "http://localhost:9090/metrics" 2>/dev/null | head -15
    echo ""

    log_info "Smoke test completed successfully."
    return 0
}

main "$@"
