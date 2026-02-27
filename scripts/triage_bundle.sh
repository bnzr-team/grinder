#!/usr/bin/env bash
# Triage Bundle — one-command snapshot for first-response diagnostics.
#
# Collects readyz, key metrics, logs, and env fingerprint into a single
# text file that can be pasted into Slack/PR/ticket.
#
# Usage: bash scripts/triage_bundle.sh [OPTIONS]
#
# Options:
#   --out <path>            Output file (default: /tmp/triage_bundle_<ts>.{txt,tgz})
#   --metrics-url <url>     Metrics endpoint (default: http://localhost:9090/metrics)
#   --readyz-url <url>      Readyz endpoint (default: http://localhost:9090/readyz)
#   --log-lines <N>         Tail N log lines (default: 300)
#   --service <name>        Systemd unit name (default: grinder)
#   --compose <path>        Docker compose file (optional)
#   --compact               Print only readyz + next-steps (for PR comments)
#   --mode <mode>           ci|local|prod|auto (default: auto)
#   --bundle-format <fmt>   txt|tgz (default: txt)
#
# Modes:
#   auto  - detect from environment (GITHUB_ACTIONS → ci, PROMETHEUS_URL → prod, else local)
#   ci    - CI context; reads PROMETHEUS_URL/GRINDER_BASE_URL env vars for URL defaults
#   local - developer machine; ignores env vars, uses localhost defaults
#   prod  - production; reads PROMETHEUS_URL/GRINDER_BASE_URL env vars for URL defaults
#
# Exit codes:
#   0 - Bundle generated (even if some checks failed)
#   1 - Could not create output file
#   2 - Usage / argument error

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────
METRICS_URL="http://localhost:9090/metrics"
READYZ_URL="http://localhost:9090/readyz"
METRICS_URL_EXPLICIT="false"
READYZ_URL_EXPLICIT="false"
LOG_LINES=300
SERVICE="grinder"
COMPOSE_FILE=""
COMPACT="false"
MODE="auto"
BUNDLE_FORMAT="txt"
OUT=""

# ── Parse flags ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)            OUT="$2";            shift 2 ;;
    --metrics-url)    METRICS_URL="$2";    METRICS_URL_EXPLICIT="true"; shift 2 ;;
    --readyz-url)     READYZ_URL="$2";     READYZ_URL_EXPLICIT="true"; shift 2 ;;
    --log-lines)      LOG_LINES="$2";      shift 2 ;;
    --service)        SERVICE="$2";        shift 2 ;;
    --compose)        COMPOSE_FILE="$2";   shift 2 ;;
    --compact)        COMPACT="true";      shift   ;;
    --mode)           MODE="$2";           shift 2 ;;
    --bundle-format)  BUNDLE_FORMAT="$2";  shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

# ── Resolve mode ──────────────────────────────────────────────────────
if [[ "${MODE}" == "auto" ]]; then
  if [[ "${GITHUB_ACTIONS:-}" == "true" || "${GITHUB_ACTIONS:-}" == "1" ]]; then
    MODE="ci"
  elif [[ -n "${GRINDER_BASE_URL:-}" || -n "${PROMETHEUS_URL:-}" ]]; then
    MODE="prod"
  else
    MODE="local"
  fi
fi

# ── Env var URL overrides (prod/ci only, unless explicit flag) ────────
if [[ "${MODE}" == "prod" || "${MODE}" == "ci" ]]; then
  if [[ "${METRICS_URL_EXPLICIT}" == "false" && -n "${PROMETHEUS_URL:-}" ]]; then
    METRICS_URL="${PROMETHEUS_URL}/metrics"
  fi
  if [[ "${READYZ_URL_EXPLICIT}" == "false" && -n "${GRINDER_BASE_URL:-}" ]]; then
    READYZ_URL="${GRINDER_BASE_URL}/readyz"
  fi
fi

# ── Output defaults ───────────────────────────────────────────────────
if [[ -z "${OUT}" ]]; then
  if [[ "${BUNDLE_FORMAT}" == "tgz" ]]; then
    OUT="/tmp/triage_bundle_$(date +%Y%m%d_%H%M%S).tgz"
  else
    OUT="/tmp/triage_bundle_$(date +%Y%m%d_%H%M%S).txt"
  fi
fi

# Ensure output directory exists
mkdir -p "$(dirname "${OUT}")" || { echo "ERROR: cannot create output dir for ${OUT}" >&2; exit 1; }

# ── Working directory + manifest ──────────────────────────────────────
OUTDIR="$(mktemp -d)"

COMPACT_INIT_FLAG=""
[[ "${COMPACT}" == "true" ]] && COMPACT_INIT_FLAG="--compact"

python3 scripts/triage_manifest.py init \
  --mode "${MODE}" \
  --out "${OUTDIR}/triage_manifest.json" \
  --metrics-url "${METRICS_URL}" \
  --readyz-url "${READYZ_URL}" \
  --log-lines "${LOG_LINES}" \
  --service "${SERVICE}" \
  --bundle-format "${BUNDLE_FORMAT}" \
  ${COMPACT_INIT_FLAG} >/dev/null 2>&1 || true

# ── Helpers ───────────────────────────────────────────────────────────

# Save original fds for restoration after all output
exec 3>&1 4>&2

# Combined text goes to either the final OUT (txt) or OUTDIR (tgz)
if [[ "${BUNDLE_FORMAT}" == "tgz" ]]; then
  exec > >(tee "${OUTDIR}/triage_bundle.txt") 2>&1
else
  exec > >(tee "${OUT}") 2>&1
fi

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

# Record an artifact in the manifest (silent — no output to combined text)
manifest_artifact() {
  local name="$1" file="$2" ok="$3" cmd_label="$4" error_msg="${5:-}"
  local bytes
  bytes=$(wc -c < "${OUTDIR}/${file}" 2>/dev/null | tr -d ' ' || echo "0")
  python3 scripts/triage_manifest.py append \
    --manifest "${OUTDIR}/triage_manifest.json" \
    --name "${name}" --path "${file}" --ok "${ok}" \
    --cmd "${cmd_label}" --bytes "${bytes}" --error "${error_msg}" >/dev/null 2>&1 || true
}

# Record a warning in the manifest
manifest_warning() {
  python3 scripts/triage_manifest.py append \
    --manifest "${OUTDIR}/triage_manifest.json" \
    --name "_warning" --path "" --ok "1" --cmd "" \
    --warning "$1" >/dev/null 2>&1 || true
}

# Record a next-step in the manifest
manifest_next_step() {
  python3 scripts/triage_manifest.py append \
    --manifest "${OUTDIR}/triage_manifest.json" \
    --name "_next_step" --path "" --ok "1" --cmd "" \
    --next-step "$1" >/dev/null 2>&1 || true
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

# ── Fetch metrics (always — needed for NEXT STEPS) ───────────────────
TMP_METRICS=$(mktemp)
trap 'rm -f "${TMP_METRICS}"; rm -rf "${OUTDIR}"' EXIT

set +e
curl -fsS "${METRICS_URL}" > "${TMP_METRICS}" 2>&1
METRICS_RC=$?
set -e

# Save metrics as individual artifact
cp "${TMP_METRICS}" "${OUTDIR}/metrics.txt" 2>/dev/null || true
if [[ ${METRICS_RC} -eq 0 ]]; then
  manifest_artifact "metrics" "metrics.txt" "1" "curl -fsS ${METRICS_URL}"
else
  manifest_artifact "metrics" "metrics.txt" "0" "curl -fsS ${METRICS_URL}" "exit=${METRICS_RC}"
  manifest_warning "metrics endpoint unreachable (${METRICS_URL})"
fi

if [[ "${COMPACT}" != "true" ]]; then

# ── B) ENV FINGERPRINT ────────────────────────────────────────────────
section "ENV FINGERPRINT"
if python3 -c "import scripts.env_fingerprint" >/dev/null 2>&1; then
  run "env_fingerprint" python3 -m scripts.env_fingerprint
  # Individual artifact
  python3 -m scripts.env_fingerprint > "${OUTDIR}/env_fingerprint.txt" 2>&1 || true
  manifest_artifact "env_fingerprint" "env_fingerprint.txt" "1" "python3 -m scripts.env_fingerprint"
else
  echo "N/A (scripts.env_fingerprint not importable)"
  echo "N/A" > "${OUTDIR}/env_fingerprint.txt"
  manifest_artifact "env_fingerprint" "env_fingerprint.txt" "0" "python3 -m scripts.env_fingerprint" "not importable"
fi

# ── C) READYZ ─────────────────────────────────────────────────────────
section "READYZ"
curl_block "readyz" "${READYZ_URL}"

# Individual readyz artifact
set +e
curl -sS "${READYZ_URL}" > "${OUTDIR}/readyz.txt" 2>&1
READYZ_CURL_RC=$?
set -e
if [[ ${READYZ_CURL_RC} -eq 0 ]]; then
  manifest_artifact "readyz" "readyz.txt" "1" "curl -sS ${READYZ_URL}"
else
  manifest_artifact "readyz" "readyz.txt" "0" "curl -sS ${READYZ_URL}" "exit=${READYZ_CURL_RC}"
fi

# Also try healthz (same port, different path)
HEALTHZ_URL="${READYZ_URL/readyz/healthz}"
if [[ "${HEALTHZ_URL}" != "${READYZ_URL}" ]]; then
  curl_block "healthz" "${HEALTHZ_URL}"
  set +e
  curl -sS "${HEALTHZ_URL}" > "${OUTDIR}/healthz.txt" 2>&1
  HEALTHZ_CURL_RC=$?
  set -e
  if [[ ${HEALTHZ_CURL_RC} -eq 0 ]]; then
    manifest_artifact "healthz" "healthz.txt" "1" "curl -sS ${HEALTHZ_URL}"
  else
    manifest_artifact "healthz" "healthz.txt" "0" "curl -sS ${HEALTHZ_URL}" "exit=${HEALTHZ_CURL_RC}"
  fi
fi

# ── D) KEY METRICS ────────────────────────────────────────────────────
section "METRICS"

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

# ── D2) CONNECTIVITY DIAG (when metrics unavailable) ─────────────────
if [[ ${METRICS_RC} -ne 0 ]]; then
  section "CONNECTIVITY DIAG"
  METRICS_PORT="${METRICS_URL##*:}"
  METRICS_PORT="${METRICS_PORT%%/*}"

  echo "--- port ${METRICS_PORT} listeners ---"
  if command -v ss >/dev/null 2>&1; then
    set +e; ss -lntp 2>/dev/null | grep -E ":${METRICS_PORT}\b" || echo "(port ${METRICS_PORT} not listening)"; set -e
  elif command -v netstat >/dev/null 2>&1; then
    set +e; netstat -lntp 2>/dev/null | grep -E ":${METRICS_PORT}\b" || echo "(port ${METRICS_PORT} not listening)"; set -e
  else
    echo "ss/netstat: N/A"
  fi

  echo ""
  echo "--- grinder processes ---"
  if command -v pgrep >/dev/null 2>&1; then
    set +e; pgrep -af 'grinder|run_trading' 2>/dev/null || echo "(no grinder processes found)"; set -e
  else
    set +e; ps aux 2>/dev/null | grep -i 'grinder\|run_trading' | grep -v grep || echo "(no grinder processes found)"; set -e
  fi

  echo ""
  echo "--- docker containers ---"
  if command -v docker >/dev/null 2>&1; then
    set +e; docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null | grep -i grinder || echo "(no grinder containers)"; set -e
  else
    echo "docker: N/A"
  fi
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

fi # end of COMPACT != true guard (sections B-E)

# ── Compact: one-line readyz status ──────────────────────────────────
if [[ "${COMPACT}" == "true" ]]; then
  set +e
  READYZ_HTTP=$(curl -sS -o /dev/null -w '%{http_code}' "${READYZ_URL}" 2>/dev/null || echo "000")
  set -e
  echo "readyz: ${READYZ_HTTP} (${READYZ_URL})"
  if [[ ${METRICS_RC} -eq 0 ]]; then
    echo "metrics: OK"
  else
    echo "metrics: UNAVAILABLE"
    echo "  check: process running + port 9090 listening + logs (docker/journalctl)"
  fi
  # Compact readyz artifact
  echo "${READYZ_HTTP}" > "${OUTDIR}/readyz_compact.txt"
  manifest_artifact "readyz_compact" "readyz_compact.txt" "1" "curl -sS -o /dev/null -w '%{http_code}' ${READYZ_URL}"
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
    manifest_next_step "READYZ returned ${READYZ_HTTP} — see 02_HEALTH_TRIAGE.md#ReadyzNotReady"
    HINTS=$((HINTS + 1))
  fi

  if [[ "${ENGINE_INIT}" == "0" && "${GRINDER_UP}" == "1" ]]; then
    echo "- Engine NOT initialized but process is up"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # EngineInitDown"
    manifest_next_step "Engine NOT initialized — see 02_HEALTH_TRIAGE.md#EngineInitDown"
    HINTS=$((HINTS + 1))
  fi

  if [[ "${FUTURES_HTTP}" != "0" && -n "${FUTURES_HTTP}" ]]; then
    echo "- Futures HTTP requests detected (${FUTURES_HTTP} total)"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # FuturesHttpRequestsDetected"
    echo "  -> Check dashboard: Route+Method panel"
    manifest_next_step "Futures HTTP requests detected (${FUTURES_HTTP}) — see 02_HEALTH_TRIAGE.md#FuturesHttpRequestsDetected"
    HINTS=$((HINTS + 1))
  fi

  if [[ "${FILL_BLOCKS_INT}" -ge 10 ]] 2>/dev/null; then
    echo "- Fill-prob blocks_total = ${FILL_BLOCKS} (elevated)"
    echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md # FillProbBlocksHigh"
    echo "  -> Note: exact rate requires PromQL increase() in Prometheus"
    manifest_next_step "Fill-prob blocks_total=${FILL_BLOCKS} — see 02_HEALTH_TRIAGE.md#FillProbBlocksHigh"
    HINTS=$((HINTS + 1))
  fi

  if [[ ${HINTS} -eq 0 ]]; then
    echo "No obvious issues detected from metrics snapshot."
    echo "For rate-based alerts, check Prometheus/Grafana dashboards."
  fi
else
  echo "- Metrics endpoint unreachable (${METRICS_URL})"
  echo "  Quick diagnostics (copy-paste):"
  echo "    ss -lntp | grep ':9090'              # is port listening?"
  echo "    pgrep -af 'grinder|run_trading'      # is process alive?"
  echo "    docker ps | grep -i grinder          # running in docker?"
  echo "    journalctl -u grinder -n 50 --no-pager  # systemd logs"
  echo "    curl -sS http://localhost:9090/readyz # readyz probe"
  echo ""
  echo "  Common causes:"
  echo "    1. Trading loop not started (check deployment / systemd / docker)"
  echo "    2. Port 9090 occupied by another process (ss -lntp | grep :9090)"
  echo "    3. Metrics endpoint on different port (check --metrics-port flag)"
  echo "    4. Firewall / network policy blocking localhost"
  echo "  -> See: docs/runbooks/02_HEALTH_TRIAGE.md"
  manifest_next_step "Metrics endpoint unreachable — see 02_HEALTH_TRIAGE.md"
fi

echo ""
echo "===== END TRIAGE BUNDLE ====="
echo "Bundle saved to: ${OUT}"

# ── Package ───────────────────────────────────────────────────────────

# Restore original stdout/stderr (closes tee subprocess)
exec 1>&3 2>&4
sleep 0.2

if [[ "${BUNDLE_FORMAT}" == "tgz" ]]; then
  # Pack without ./ prefix so `tar -xzf ... -O triage_bundle.txt` works
  (cd "${OUTDIR}" && tar -czf "${OUT}" *)
else
  # txt mode: tee already wrote to ${OUT}; copy manifest alongside
  cp "${OUTDIR}/triage_manifest.json" "${OUT%.txt}_manifest.json" 2>/dev/null || true
fi
