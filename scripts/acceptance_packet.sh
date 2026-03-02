#!/usr/bin/env bash
# Acceptance Packet Generator for GRINDER
#
# One-command "ready for ACCEPT" generator.
# Waits for CI, runs quality gates, generates artifacts, validates replay if required.
#
# Usage: ./scripts/acceptance_packet.sh [--require-main-base] <PR_NUMBER>
#
# Options:
#   --require-main-base   Fail if PR base is not main/master (for final PRs)
#
# Exit codes:
#   0 - All checks passed, ready for ACCEPT
#   1 - Some check failed (CI, gates, replay, or base requirement)
#   2 - Usage error

set -euo pipefail

# Parse options
REQUIRE_MAIN_BASE="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --require-main-base)
      REQUIRE_MAIN_BASE="true"
      shift
      ;;
    -*)
      echo "Unknown option: $1"
      echo "Usage: ./scripts/acceptance_packet.sh [--require-main-base] <PR_NUMBER>"
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

PR_NUMBER="${1:-}"
if [[ -z "${PR_NUMBER}" ]]; then
  echo "Usage: ./scripts/acceptance_packet.sh [--require-main-base] <PR_NUMBER>"
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Activate venv if available (ensures correct Python environment)
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

# Colors for terminal (disabled if not tty)
if [[ -t 1 ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  NC='\033[0m'
else
  RED=''
  GREEN=''
  YELLOW=''
  NC=''
fi

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Track overall status
PACKET_STATUS="PASS"
FAILED_CHECKS=()

fail_check() {
  PACKET_STATUS="FAIL"
  FAILED_CHECKS+=("$1")
  log_error "$1"
}

echo "== ACCEPTANCE PACKET: IDENTITY =="
# Use gh api (REST) instead of gh pr view (GraphQL) to avoid
# "Projects (classic) is being deprecated" failures.
REPO_NWO_ID="$(gh repo view --json nameWithOwner -q '.nameWithOwner')"
PR_JSON="$(gh api "repos/${REPO_NWO_ID}/pulls/${PR_NUMBER}" --jq '{
  url: .html_url,
  state: .state,
  mergedAt: .merged_at,
  mergeCommit: .merge_commit_sha,
  baseRefName: .base.ref,
  headRefName: .head.ref,
  title: .title,
  number: .number
}')"
echo "${PR_JSON}" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))'
echo

PR_URL="$(echo "${PR_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
STATE="$(echo "${PR_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"
BASE_REF="$(echo "${PR_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["baseRefName"])')"
HEAD_REF="$(echo "${PR_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["headRefName"])')"

echo "PR URL: ${PR_URL}"
echo "PR STATE: ${STATE}"
echo "Base: ${BASE_REF}"
echo "Head: ${HEAD_REF}"
echo
echo "NOTE: Paste this output verbatim in full. Do NOT summarize."
echo

# == MERGE SAFETY: Base branch check + PR chain detection ==
echo "== ACCEPTANCE PACKET: MERGE SAFETY =="
IS_MAIN_BASE="false"
READY_FOR_FINAL_MERGE="false"
PR_CHAIN=()
CHAIN_VALID="true"

if [[ "${BASE_REF}" == "main" || "${BASE_REF}" == "master" ]]; then
  IS_MAIN_BASE="true"
  READY_FOR_FINAL_MERGE="true"
  echo "merge_type: DIRECT"
  echo "base_branch: ${BASE_REF} (main branch)"
  echo "ready_for_final_merge: true"
  log_info "PR targets main branch directly — ready for final merge"
else
  echo "merge_type: STACKED"
  echo "base_branch: ${BASE_REF} (NOT main)"
  echo "ready_for_final_merge: false"
  log_warn "STACKED PR detected: base is '${BASE_REF}', not main"

  if [[ "${REQUIRE_MAIN_BASE}" == "true" ]]; then
    fail_check "BASE_NOT_MAIN: --require-main-base specified but base is '${BASE_REF}'"
  fi
fi
echo

# Get list of changed files
echo "== ACCEPTANCE PACKET: CHANGED FILES =="
REPO_NWO="$(gh repo view --json nameWithOwner -q '.nameWithOwner')"
if [[ -z "${REPO_NWO}" ]]; then
  fail_check "REPO_DETECT_FAILED: Could not determine repository"
  CHANGED_FILES=""
else
  CHANGED_FILES="$(gh api "/repos/${REPO_NWO}/pulls/${PR_NUMBER}/files" --paginate --jq '.[].filename' 2>&1)" || {
    fail_check "FILES_API_FAILED: gh api failed"
    CHANGED_FILES=""
  }
fi

if [[ -z "${CHANGED_FILES}" && "${PACKET_STATUS}" == "PASS" ]]; then
  fail_check "FILES_EMPTY: PR file list is empty"
fi

echo "${CHANGED_FILES}"
echo

# Detect if replay is required
require_replay() {
  local files="$1"
  echo "${files}" | grep -qE '^(scripts/run_replay\.py|scripts/run_backtest\.py|tests/backtest/|tests/fixtures/|src/grinder/policies/|src/grinder/execution/|src/grinder/risk/|src/grinder/backtest/)' && return 0
  return 1
}

if require_replay "${CHANGED_FILES}"; then
  REPLAY_REQUIRED="true"
  log_info "Replay verification REQUIRED (PR touches replay-related files)"
else
  REPLAY_REQUIRED="false"
  log_info "Replay verification not required"
fi
echo "replay_required: ${REPLAY_REQUIRED}"
echo

echo "== ACCEPTANCE PACKET: CI =="
log_info "Waiting for CI checks to complete..."

MAX_WAIT=600
POLL_INTERVAL=10
WAITED=0

while true; do
  CHECKS_OUTPUT="$(gh pr checks "${PR_NUMBER}" 2>&1 || true)"
  FILTERED_CHECKS="$(echo "${CHECKS_OUTPUT}" | grep -v "^acceptance-packet" | grep -v "^proof-guard" || true)"
  echo "${FILTERED_CHECKS}"

  if echo "${FILTERED_CHECKS}" | grep -q "pending"; then
    if [[ ${WAITED} -ge ${MAX_WAIT} ]]; then
      fail_check "CI_TIMEOUT: Checks still pending after ${MAX_WAIT}s"
      break
    fi
    log_warn "Checks still pending, waiting ${POLL_INTERVAL}s... (${WAITED}/${MAX_WAIT}s)"
    sleep "${POLL_INTERVAL}"
    WAITED=$((WAITED + POLL_INTERVAL))
    continue
  fi

  if echo "${FILTERED_CHECKS}" | grep -qE "(fail|cancelled)"; then
    fail_check "CI_FAILED: One or more checks failed"
    break
  fi

  log_info "All CI checks passed"
  break
done
echo

echo "== ACCEPTANCE PACKET: GATES =="

echo "--- ruff check . ---"
if ruff check .; then
  log_info "ruff: PASS"
else
  fail_check "RUFF_FAILED"
fi
echo

echo "--- ruff format --check . ---"
if ruff format --check .; then
  log_info "ruff format: PASS"
else
  fail_check "RUFF_FORMAT_FAILED"
fi
echo

echo "--- mypy . ---"
if mypy .; then
  log_info "mypy: PASS"
else
  fail_check "MYPY_FAILED"
fi
echo

echo "--- pytest -q ---"
if pytest -q; then
  log_info "pytest: PASS"
else
  fail_check "PYTEST_FAILED"
fi
echo

echo "== ACCEPTANCE PACKET: PR DIFF =="
echo "--- gh pr diff ${PR_NUMBER} ---"
gh pr diff "${PR_NUMBER}" || fail_check "PR_DIFF_FAILED"
echo

echo "== ACCEPTANCE PACKET: REPLAY DETERMINISM =="
if [[ "${REPLAY_REQUIRED}" == "true" ]]; then
  log_info "Running replay determinism verification..."

  # Create temp directory for fixture
  TEMP_FIXTURE="$(mktemp -d)"
  trap 'rm -rf "${TEMP_FIXTURE}"' EXIT

  echo "--- Generating synthetic fixture ---"
  if python3 -m scripts.generate_fixture --symbols BTCUSDT --duration-s 2 --out-dir "${TEMP_FIXTURE}" 2>&1; then
    log_info "Fixture generated successfully"
  else
    fail_check "FIXTURE_GEN_FAILED: Could not generate synthetic fixture"
  fi
  echo

  # Run replay twice
  echo "--- Replay run #1 ---"
  REPLAY1_OUTPUT="$(python3 -m scripts.run_replay --fixture "${TEMP_FIXTURE}" -v 2>&1)"
  echo "${REPLAY1_OUTPUT}"
  DIGEST1="$(echo "${REPLAY1_OUTPUT}" | grep "Output digest:" | awk '{print $NF}' || true)"
  echo

  echo "--- Replay run #2 ---"
  REPLAY2_OUTPUT="$(python3 -m scripts.run_replay --fixture "${TEMP_FIXTURE}" -v 2>&1)"
  echo "${REPLAY2_OUTPUT}"
  DIGEST2="$(echo "${REPLAY2_OUTPUT}" | grep "Output digest:" | awk '{print $NF}' || true)"
  echo

  echo "--- Digest verification ---"
  echo "Run #1 digest: ${DIGEST1}"
  echo "Run #2 digest: ${DIGEST2}"

  if [[ -n "${DIGEST1}" && "${DIGEST1}" == "${DIGEST2}" ]]; then
    echo "ALL DIGESTS MATCH"
    log_info "Replay determinism: PASS"
  else
    fail_check "REPLAY_MISMATCH: Digests do not match"
  fi
else
  echo "replay_required: false"
  echo "Skipping replay verification (PR does not touch replay-related files)"
fi
echo

echo "== ACCEPTANCE PACKET: END =="

if [[ "${PACKET_STATUS}" == "FAIL" ]]; then
  echo "RESULT: FAIL"
  echo "PR: #${PR_NUMBER}"
  echo "URL: ${PR_URL}"
  echo "State: ${STATE}"
  echo "Base: ${BASE_REF}"
  echo "Merge type: $([ "${IS_MAIN_BASE}" == "true" ] && echo "DIRECT" || echo "STACKED")"
  echo "Ready for review: false"
  echo "Ready for final merge: false"
  echo "Replay required: ${REPLAY_REQUIRED}"
  echo ""
  echo "Failed checks:"
  for check in "${FAILED_CHECKS[@]}"; do
    echo "  - ${check}"
  done
  exit 1
else
  echo "RESULT: PASS"
  echo "PR: #${PR_NUMBER}"
  echo "URL: ${PR_URL}"
  echo "State: ${STATE}"
  echo "Base: ${BASE_REF}"
  echo "Merge type: $([ "${IS_MAIN_BASE}" == "true" ] && echo "DIRECT" || echo "STACKED")"
  echo "Ready for review: true"
  echo "Ready for final merge: ${READY_FOR_FINAL_MERGE}"
  echo "Replay required: ${REPLAY_REQUIRED}"
  echo ""
  if [[ "${IS_MAIN_BASE}" == "true" ]]; then
    echo "All checks passed. Ready for REVIEW and FINAL MERGE to main."
  else
    echo "All checks passed. Ready for REVIEW."
    echo "STACKED PR: NOT ready for final merge."
  fi
  exit 0
fi
