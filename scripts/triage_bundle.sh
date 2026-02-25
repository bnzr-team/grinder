#!/usr/bin/env bash
# Triage Bundle — one-command snapshot for first-response diagnostics.
#
# Collects readyz, key metrics, logs, and env fingerprint into a single
# text file that can be pasted into Slack/PR/ticket.
#
# Usage: bash scripts/triage_bundle.sh [OPTIONS]
#
# Options:
#   --out <path>          Output file (default: /tmp/triage_bundle_<ts>.txt)
#   --metrics-url <url>   Metrics endpoint (default: http://localhost:9090/metrics)
#   --readyz-url <url>    Readyz endpoint (default: http://localhost:9090/readyz)
#   --log-lines <N>       Tail N log lines (default: 300)
#   --service <name>      Systemd unit name (default: grinder)
#   --compose <path>      Docker compose file (optional)
#
# Exit codes:
#   0 - Bundle generated (even if some checks failed)
#   1 - Could not create output file

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────
METRICS_URL="http://localhost:9090/metrics"
READYZ_URL="http://localhost:9090/readyz"
LOG_LINES=300
SERVICE="grinder"
COMPOSE_FILE=""
OUT=""

# ── Parse flags ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)          OUT="$2";          shift 2 ;;
    --metrics-url)  METRICS_URL="$2";  shift 2 ;;
    --readyz-url)   READYZ_URL="$2";   shift 2 ;;
    --log-lines)    LOG_LINES="$2";    shift 2 ;;
    --service)      SERVICE="$2";      shift 2 ;;
    --compose)      COMPOSE_FILE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "${OUT}" ]]; then
  OUT="/tmp/triage_bundle_$(date +%Y%m%d_%H%M%S).txt"
fi

# Ensure output directory exists
mkdir -p "$(dirname "${OUT}")" || { echo "ERROR: cannot create output dir for ${OUT}" >&2; exit 1; }

# ── Helpers ───────────────────────────────────────────────────────────

# All output goes to the bundle file AND stdout
exec > >(tee "${OUT}") 2>&1

section() {
  echo ""
  echo "===== $1 ====="
}

# Run a command, capture output, never fail the script
run() {
  local label="$1"; shift
  echo "--- ${label} ---"
  echo "\$ $*"
  set +e
  "$@" 2>&1
  local rc=$?
  set -e
  if [[ ${rc} -ne 0 ]]; then
    echo "EXIT=${rc}"
  fi
  return 0
}

# curl a URL, show HTTP status + body, never fail
curl_block() {
  local label="$1"
  local url="$2"
  echo "--- ${label} ---"
  echo "\$ curl -sS -w '\\nHTTP_STATUS:%{http_code}' ${url}"
  set +e
  local output
  output=$(curl -sS -w '\nHTTP_STATUS:%{http_code}' "${url}" 2>&1)
  local rc=$?
  set -e
  if [[ ${rc} -ne 0 ]]; then
    echo "CURL_FAILED (exit ${rc}): ${output}"
  else
    echo "${output}"
  fi
  return 0
}

# ── A) METADATA ───────────────────────────────────────────────────────
section "METADATA"
echo "timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "hostname:  $(hostname 2>/dev/null || echo 'N/A')"
echo "uname:     $(uname -a 2>/dev/null || echo 'N/A')"
echo "user:      $(whoami 2>/dev/null || echo 'N/A')"
echo "pwd:       $(pwd)"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "git_root:  $(git rev-parse --show-toplevel 2>/dev/null)"
  echo "git_head:  $(git log --oneline -1 2>/dev/null || echo 'N/A')"
  echo "git_branch: $(git branch --show-current 2>/dev/null || echo 'N/A')"
  echo "git_dirty: $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') files"
fi

# ── B) ENV FINGERPRINT ────────────────────────────────────────────────
section "ENV FINGERPRINT"
if python3 -c "import scripts.env_fingerprint" >/dev/null 2>&1; then
  run "env_fingerprint" python3 -m scripts.env_fingerprint
else
  echo "N/A (scripts.env_fingerprint not importable)"
fi

# ── C) READYZ ─────────────────────────────────────────────────────────
section "READYZ"
curl_block "readyz" "${READYZ_URL}"

# Also try healthz (same port, different path)
HEALTHZ_URL="${READYZ_URL/readyz/healthz}"
if [[ "${HEALTHZ_URL}" != "${READYZ_URL}" ]]; then
  curl_block "healthz" "${HEALTHZ_URL}"
fi

# ── D) KEY METRICS ────────────────────────────────────────────────────
section "METRICS"

TMP_METRICS=$(mktemp)
trap 'rm -f "${TMP_METRICS}"' EXIT

set +e
curl -fsS "${METRICS_URL}" > "${TMP_METRICS}" 2>&1
METRICS_RC=$?
set -e

if [[ ${METRICS_RC} -ne 0 ]]; then
  echo "METRICS_FETCH_FAILED (exit ${METRICS_RC})"
  echo "URL: ${METRICS_URL}"
  cat "${TMP_METRICS}"
else
  METRICS_LINES=$(wc -l < "${TMP_METRICS}")
  echo "Fetched ${METRICS_LINES} lines from ${METRICS_URL}"
  echo ""

  echo "--- Core ---"
  grep -E '^grinder_(up|uptime_seconds)\b' "${TMP_METRICS}" || echo "(none)"
  echo ""

  echo "--- Engine init ---"
  grep -E '^grinder_live_engine_initialized\b' "${TMP_METRICS}" || echo "(none)"
  echo ""

  echo "--- Readyz gauges ---"
  grep -E '^grinder_readyz_' "${TMP_METRICS}" || echo "(none)"
  echo ""

  echo "--- HA role ---"
  grep -E '^grinder_ha_(role|is_leader)\b' "${TMP_METRICS}" || echo "(none)"
  echo ""

  echo "--- Port HTTP requests (futures safety) ---"
  grep -E '^grinder_port_http_requests_total\b' "${TMP_METRICS}" | grep 'port="futures"' || echo "(none)"
  echo ""

  echo "--- Port order attempts ---"
  grep -E '^grinder_port_order_attempts_total\b' "${TMP_METRICS}" || echo "(none)"
  echo ""

  echo "--- Fill-prob gate ---"
  grep -E '^grinder_router_fill_prob_(blocks_total|cb_trips_total|enforce_enabled|enforce_allowlist_enabled|auto_threshold_bps)\b' "${TMP_METRICS}" || echo "(none)"
  echo ""

  echo "--- Fill model ---"
  grep -E '^grinder_ml_fill_(prob_bps_last|model_loaded)\b' "${TMP_METRICS}" || echo "(none)"
fi

# ── E) LOGS ───────────────────────────────────────────────────────────
section "LOGS"

LOGS_FOUND="false"

# Try 1: docker compose
if command -v docker >/dev/null 2>&1; then
  if [[ -n "${COMPOSE_FILE}" ]]; then
    echo "--- docker compose logs (${COMPOSE_FILE}) ---"
    set +e
    docker compose -f "${COMPOSE_FILE}" logs --tail "${LOG_LINES}" --timestamps 2>&1
    if [[ $? -eq 0 ]]; then LOGS_FOUND="true"; fi
    set -e
  elif docker compose ps >/dev/null 2>&1; then
    echo "--- docker compose logs ---"
    set +e
    docker compose logs --tail "${LOG_LINES}" --timestamps 2>&1
    if [[ $? -eq 0 ]]; then LOGS_FOUND="true"; fi
    set -e
  fi
fi

# Try 2: journalctl
if [[ "${LOGS_FOUND}" == "false" ]] && command -v journalctl >/dev/null 2>&1; then
  echo "--- journalctl -u ${SERVICE} ---"
  set +e
  journalctl -u "${SERVICE}" -n "${LOG_LINES}" --no-pager 2>&1
  if [[ $? -eq 0 ]]; then LOGS_FOUND="true"; fi
  set -e
fi

# Try 3: /tmp log files
if [[ "${LOGS_FOUND}" == "false" ]]; then
  LOG_FILES=$(ls -1 /tmp/*grinder*.log 2>/dev/null || true)
  if [[ -n "${LOG_FILES}" ]]; then
    echo "--- /tmp grinder log files ---"
    echo "${LOG_FILES}"
    while IFS= read -r f; do
      echo ""
      echo "--- tail -${LOG_LINES} ${f} ---"
      tail -n "${LOG_LINES}" "${f}" 2>/dev/null || echo "(read failed)"
    done <<< "${LOG_FILES}"
    LOGS_FOUND="true"
  fi
fi

if [[ "${LOGS_FOUND}" == "false" ]]; then
  echo "LOGS: not found (no docker/journalctl/tmp logs)"
fi

# ── F) NEXT STEPS ─────────────────────────────────────────────────────
section "NEXT STEPS"

if [[ ${METRICS_RC} -eq 0 ]]; then
  # Parse readyz HTTP status from earlier curl output
  READYZ_HTTP=$(curl -sS -o /dev/null -w '%{http_code}' "${READYZ_URL}" 2>/dev/null || echo "000")

  ENGINE_INIT=$(grep -E '^grinder_live_engine_initialized\b' "${TMP_METRICS}" 2>/dev/null | awk '{print $2}' || echo "")
  GRINDER_UP=$(grep -E '^grinder_up\b' "${TMP_METRICS}" 2>/dev/null | awk '{print $2}' || echo "")
  FUTURES_LINES=$(grep -E '^grinder_port_http_requests_total\b' "${TMP_METRICS}" 2>/dev/null | grep 'port="futures"' || true)
  if [[ -n "${FUTURES_LINES}" ]]; then
    FUTURES_HTTP=$(echo "${FUTURES_LINES}" | awk '{sum+=$2} END {print int(sum)}')
  else
    FUTURES_HTTP="0"
  fi
  FILL_BLOCKS=$(grep -E '^grinder_router_fill_prob_blocks_total\b' "${TMP_METRICS}" 2>/dev/null | awk '{print $2}' || echo "0")
  # Strip decimal for integer comparison (counters may show as "0.0")
  FILL_BLOCKS_INT=${FILL_BLOCKS%%.*}
  FILL_BLOCKS_INT=${FILL_BLOCKS_INT:-0}

  HINTS=0

  if [[ "${READYZ_HTTP}" != "200" ]]; then
    echo "- READYZ returned ${READYZ_HTTP} (not 200)"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # ReadyzNotReady"
    HINTS=$((HINTS + 1))
  fi

  if [[ "${ENGINE_INIT}" == "0" && "${GRINDER_UP}" == "1" ]]; then
    echo "- Engine NOT initialized but process is up"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # EngineInitDown"
    HINTS=$((HINTS + 1))
  fi

  if [[ "${FUTURES_HTTP}" != "0" && -n "${FUTURES_HTTP}" ]]; then
    echo "- Futures HTTP requests detected (${FUTURES_HTTP} total)"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # FuturesHttpRequestsDetected"
    echo "  -> Check dashboard: Route+Method panel"
    HINTS=$((HINTS + 1))
  fi

  if [[ "${FILL_BLOCKS_INT}" -ge 10 ]] 2>/dev/null; then
    echo "- Fill-prob blocks_total = ${FILL_BLOCKS} (elevated)"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # FillProbBlocksHigh"
    echo "  -> Note: exact rate requires PromQL increase() in Prometheus"
    HINTS=$((HINTS + 1))
  fi

  if [[ ${HINTS} -eq 0 ]]; then
    echo "No obvious issues detected from metrics snapshot."
    echo "For rate-based alerts, check Prometheus/Grafana dashboards."
  fi
else
  echo "Metrics unavailable — cannot auto-triage."
  echo "Check if the trading loop is running and metrics port is reachable."
fi

echo ""
echo "===== END TRIAGE BUNDLE ====="
echo "Bundle saved to: ${OUT}"
