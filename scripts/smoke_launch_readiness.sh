#!/usr/bin/env bash
# GRINDER Launch Readiness Smoke Test (Launch-01)
#
# Validates single-venue launch preconditions:
# - Container builds and starts
# - /healthz returns 200 with required JSON keys (SSOT)
# - /metrics passes full SSOT contract validation
# - /readyz responds (200 or 503) with required JSON keys (SSOT)
# - Graceful stop
#
# Usage:
#   bash scripts/smoke_launch_readiness.sh
#
# Environment:
#   SMOKE_FORCE_FAIL=1 — Force failure after startup (for FAIL demo)
#
# Exit codes:
#   0 - All checks passed
#   1 - Contract validation failure
#   2 - Infrastructure/startup failure

set -euo pipefail

COMPOSE_FILE="docker-compose.yml"
SERVICE_NAME="grinder"
MAX_WAIT_SECONDS=90
POLL_INTERVAL=5
METRICS_PORT=9090

# Temp files (cleaned up on exit)
HEALTHZ_TMP="/tmp/launch_healthz.json"
READYZ_TMP="/tmp/launch_readyz.json"
METRICS_TMP="/tmp/launch_metrics.txt"

# SSOT contract module path (relative to repo root)
CONTRACT_MODULE="src/grinder/observability/metrics_contract.py"

# Detect docker compose command (v2 plugin or v1 standalone)
if docker compose version &>/dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "ERROR: Neither 'docker compose' nor 'docker-compose' found"
    exit 2
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

die_contract() {
    log_error "CONTRACT FAILURE: $1"
    exit 1
}

die_infra() {
    log_error "INFRASTRUCTURE FAILURE: $1"
    exit 2
}

# Cleanup function — always runs on exit
cleanup() {
    local exit_code=$?
    log_info "Cleaning up..."

    if [ $exit_code -ne 0 ]; then
        log_error "Smoke test failed (exit=$exit_code). Dumping diagnostics..."
        echo ""
        echo "=== Container Status ==="
        $COMPOSE_CMD -f "$COMPOSE_FILE" ps 2>/dev/null || true
        echo ""
        echo "=== Container Logs (last 100 lines) ==="
        $COMPOSE_CMD -f "$COMPOSE_FILE" logs --no-color --tail=100 "$SERVICE_NAME" 2>/dev/null || true
    fi

    $COMPOSE_CMD -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    rm -f "$HEALTHZ_TMP" "$READYZ_TMP" "$METRICS_TMP"
    log_info "Cleanup complete."
}

trap cleanup EXIT

# Wait for endpoint to be ready (bounded poll)
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
    return 1
}

# Validate JSON keys from SSOT contract
validate_json_keys() {
    local file="$1"
    local endpoint="$2"
    local keys_attr="$3"

    python3 -c "
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('mc', '${CONTRACT_MODULE}')
mc = importlib.util.module_from_spec(spec); spec.loader.exec_module(mc)
data = json.load(open('${file}'))
required = getattr(mc, '${keys_attr}')
missing = [k for k in required if k not in data]
if missing:
    print(f'FAIL: ${endpoint} missing keys: {missing}'); sys.exit(1)
print(f'OK: ${endpoint} keys present: {required}')
"
}

# Validate /metrics against SSOT contract (no httpx needed)
validate_metrics_contract() {
    python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('mc', '${CONTRACT_MODULE}')
mc = importlib.util.module_from_spec(spec); spec.loader.exec_module(mc)
text = open('${METRICS_TMP}').read()
missing = [p for p in mc.REQUIRED_METRICS_PATTERNS if p not in text]
forbidden = [l for l in mc.FORBIDDEN_METRIC_LABELS if l in text]
print(f'Required: {len(mc.REQUIRED_METRICS_PATTERNS)} total, {len(missing)} missing')
print(f'Forbidden: {len(mc.FORBIDDEN_METRIC_LABELS)} checked, {len(forbidden)} found')
if missing:
    print('MISSING:'); [print(f'  - {m}') for m in missing]
if forbidden:
    print('FORBIDDEN:'); [print(f'  - {f}') for f in forbidden]
ok = not missing and not forbidden
print('METRICS CONTRACT: ' + ('PASS' if ok else 'FAIL'))
sys.exit(0 if ok else 1)
"
}

# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

main() {
    log_info "=== GRINDER Launch Readiness Smoke Test (Launch-01) ==="
    log_info "Compose file: $COMPOSE_FILE"
    log_info "Service: $SERVICE_NAME"
    log_info "Max wait: ${MAX_WAIT_SECONDS}s"
    echo ""

    # ── Check 0: SMOKE_FORCE_FAIL ────────────────────────────────────
    if [ "${SMOKE_FORCE_FAIL:-}" = "1" ]; then
        log_warn "SMOKE_FORCE_FAIL=1 is set — will force-fail after startup"
    fi

    # ── Check 1: Build & start grinder service only ──────────────────
    log_info "Building and starting $SERVICE_NAME service..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" up --build -d "$SERVICE_NAME" \
        || die_infra "Failed to start $SERVICE_NAME"
    echo ""

    # Force-fail after startup if requested (FAIL demo)
    if [ "${SMOKE_FORCE_FAIL:-}" = "1" ]; then
        die_contract "SMOKE_FORCE_FAIL=1 — forced failure for FAIL demo"
    fi

    # ── Check 2: /healthz ────────────────────────────────────────────
    log_info "Checking /healthz..."
    wait_for_endpoint "http://localhost:${METRICS_PORT}/healthz" "/healthz" \
        || die_infra "/healthz not available after ${MAX_WAIT_SECONDS}s"

    curl -sf "http://localhost:${METRICS_PORT}/healthz" > "$HEALTHZ_TMP" \
        || die_infra "/healthz fetch failed"
    validate_json_keys "$HEALTHZ_TMP" "/healthz" "REQUIRED_HEALTHZ_KEYS" \
        || die_contract "/healthz missing required keys"
    echo ""

    # ── Check 3: /metrics SSOT contract ──────────────────────────────
    log_info "Checking /metrics contract (SSOT)..."
    wait_for_endpoint "http://localhost:${METRICS_PORT}/metrics" "/metrics" "grinder_up" \
        || die_infra "/metrics not available after ${MAX_WAIT_SECONDS}s"

    curl -sf "http://localhost:${METRICS_PORT}/metrics" > "$METRICS_TMP" \
        || die_infra "/metrics fetch failed"
    validate_metrics_contract \
        || die_contract "/metrics SSOT contract validation failed"
    echo ""

    # ── Check 4: /readyz ─────────────────────────────────────────────
    log_info "Checking /readyz..."
    # Non-HA mode returns 503 (expected). Accept 200 or 503.
    READYZ_CODE=$(curl -s -o "$READYZ_TMP" -w "%{http_code}" \
        "http://localhost:${METRICS_PORT}/readyz") || true

    if [[ "$READYZ_CODE" != "200" && "$READYZ_CODE" != "503" ]]; then
        die_infra "/readyz unexpected HTTP $READYZ_CODE (expected 200 or 503)"
    fi
    log_info "/readyz HTTP $READYZ_CODE (OK — 200=HA-active, 503=non-HA/standby)"

    validate_json_keys "$READYZ_TMP" "/readyz" "REQUIRED_READYZ_KEYS" \
        || die_contract "/readyz missing required keys"
    echo ""

    # ── Check 5: Graceful stop ───────────────────────────────────────
    log_info "Stopping $SERVICE_NAME..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" stop "$SERVICE_NAME" \
        || die_infra "Failed to stop $SERVICE_NAME"
    log_info "Graceful stop: OK"
    echo ""

    # ── Summary ──────────────────────────────────────────────────────
    log_info "=== All checks passed ==="
    echo ""
    echo "Launch Readiness Summary:"
    echo "  /healthz ............. PASS (200, required keys present)"
    echo "  /metrics contract .... PASS (SSOT validated)"
    echo "  /readyz .............. PASS (HTTP $READYZ_CODE, required keys present)"
    echo "  Graceful stop ........ PASS"
    echo ""
    log_info "Launch readiness smoke test completed successfully."
    return 0
}

main "$@"
