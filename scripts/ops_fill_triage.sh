#!/usr/bin/env bash
# ops_fill_triage.sh -- One-command fill triage wrapper (Launch-07 PR2).
#
# Single entrypoint for all fill evidence scripts:
#   local      -> smoke_fill_ingest.sh       (FakePort, no API keys)
#   staging    -> smoke_fill_ingest_staging.sh (Gate A always; B/C if creds)
#   fire-drill -> fire_drill_fill_alerts.sh   (deterministic alert input proof)
#
# Does NOT invent a new evidence format -- runs the underlying script,
# surfaces its evidence_dir, and prints next-step pointers.
#
# Usage:
#   bash scripts/ops_fill_triage.sh <mode>
#   bash scripts/ops_fill_triage.sh -h|--help
#
# Modes:
#   local       No API keys needed. Validates metric wiring via FakePort (~5s).
#   staging     Gate A always; B/C require BINANCE_API_KEY + BINANCE_API_SECRET
#               + ALLOW_MAINNET_TRADE=1.  (~2 min full, ~10s Gate A only).
#   fire-drill  No API keys needed. Proves alert inputs are produced (~10s).
#
# Exit codes:
#   0  All checks passed
#   1  Underlying script failed
#   2  Usage error (unknown mode, bad args)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# =========================================================================
# Helpers
# =========================================================================

die() {
  echo "ERROR: $1" >&2
  exit 2
}

print_header() {
  echo ""
  echo "========================================"
  echo "  ops_fill_triage: $1"
  echo "========================================"
  echo ""
}

print_usage() {
  cat <<'USAGE'
Usage: bash scripts/ops_fill_triage.sh <mode> [options]

Modes:
  local        Local smoke test (FakePort, no API keys needed)
  staging      Staging smoke (Gate A always; B/C if creds set)
  fire-drill   Deterministic alert input proof (no API keys needed)

Options:
  -h, --help       Show this help and exit
  --no-status      Skip git status guardrail check

Examples:
  bash scripts/ops_fill_triage.sh local
  bash scripts/ops_fill_triage.sh fire-drill
  bash scripts/ops_fill_triage.sh staging
  bash scripts/ops_fill_triage.sh staging --no-status

Evidence artifacts (staging, fire-drill) are saved under .artifacts/...
(gitignored). Do not commit.

Docs:
  docs/runbooks/00_OPS_QUICKSTART.md   -- Operator quickstart
  docs/runbooks/00_EVIDENCE_INDEX.md   -- Evidence artifact index
  docs/runbooks/26_FILL_TRACKER_TRIAGE.md -- Fill tracker triage
USAGE
}

# Extract evidence_dir from script output.
# Looks for lines matching "evidence_dir: <path>".
# Returns the LAST match (scripts may print it more than once).
extract_evidence_dir() {
  local output="$1"
  local dir
  dir="$(echo "$output" | grep -oP 'evidence_dir: \K\S+' | tail -1)" || true
  echo "$dir"
}

# =========================================================================
# Argument parsing
# =========================================================================

MODE=""
SKIP_STATUS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      print_usage
      exit 0
      ;;
    --no-status)
      SKIP_STATUS=1
      shift
      ;;
    -*)
      die "Unknown option: $1 (try -h for help)"
      ;;
    *)
      if [[ -z "$MODE" ]]; then
        MODE="$1"
      else
        die "Unexpected argument: $1 (only one mode allowed)"
      fi
      shift
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  die "No mode specified. Usage: bash scripts/ops_fill_triage.sh <mode> (try -h for help)"
fi

# =========================================================================
# Mode dispatch
# =========================================================================

case "$MODE" in
  local)
    SCRIPT_PATH="$SCRIPT_DIR/smoke_fill_ingest.sh"
    LABEL="Local smoke (FakePort)"
    HAS_ARTIFACTS=0
    ;;
  staging)
    SCRIPT_PATH="$SCRIPT_DIR/smoke_fill_ingest_staging.sh"
    LABEL="Staging smoke (Gate A always; B/C if creds)"
    HAS_ARTIFACTS=1
    ;;
  fire-drill)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_fill_alerts.sh"
    LABEL="Alert input fire drill"
    HAS_ARTIFACTS=1
    ;;
  *)
    die "Unknown mode: '$MODE'. Valid modes: local, staging, fire-drill (try -h for help)"
    ;;
esac

if [[ ! -f "$SCRIPT_PATH" ]]; then
  die "Script not found: $SCRIPT_PATH"
fi

# =========================================================================
# Run underlying script
# =========================================================================

print_header "$LABEL"

START_TS=$(date +%s)
SCRIPT_RC=0

# Run script: live output to terminal, captured to temp file for parsing.
OUTPUT_FILE="$(mktemp)"
trap "rm -f '$OUTPUT_FILE'" EXIT

bash "$SCRIPT_PATH" 2>&1 | tee "$OUTPUT_FILE" || SCRIPT_RC=$?

SCRIPT_OUTPUT="$(cat "$OUTPUT_FILE")"

END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))

echo ""

# =========================================================================
# Post-run summary
# =========================================================================

if [[ "$SCRIPT_RC" -ne 0 ]]; then
  echo "========================================"
  echo "  FAILED: $MODE (exit code $SCRIPT_RC)"
  echo "  elapsed: ${ELAPSED}s"
  echo "========================================"
  echo ""
  echo "NEXT: check the output above for FAIL lines, then see:"
  echo "  docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
  echo "  docs/runbooks/00_OPS_QUICKSTART.md"
  exit 1
fi

echo "========================================"
echo "  PASSED: $MODE"
echo "  elapsed: ${ELAPSED}s"

# Surface evidence directory if applicable.
if [[ "$HAS_ARTIFACTS" -eq 1 ]]; then
  EVIDENCE_DIR="$(extract_evidence_dir "$SCRIPT_OUTPUT")"
  if [[ -n "$EVIDENCE_DIR" ]]; then
    echo "  evidence_dir: $EVIDENCE_DIR"
  else
    echo "  evidence_dir: UNKNOWN"
  fi
fi

echo "========================================"
echo ""

# Git status guardrail (for modes that produce artifacts).
if [[ "$HAS_ARTIFACTS" -eq 1 ]] && [[ "$SKIP_STATUS" -eq 0 ]]; then
  if git status --porcelain 2>/dev/null | grep -qF ".artifacts/"; then
    echo "WARNING: .artifacts/ appears in git status (gitignore may be broken)"
    git status --porcelain 2>/dev/null | grep -F ".artifacts/" | head -5
    echo ""
  fi
fi

# Artifact reminder.
if [[ "$HAS_ARTIFACTS" -eq 1 ]]; then
  echo "NOTE: artifacts live under .artifacts/... (gitignored). Do not commit."
  echo ""
  if [[ -n "${EVIDENCE_DIR:-}" ]] && [[ "$EVIDENCE_DIR" != "UNKNOWN" ]]; then
    echo "To paste evidence into a PR or incident note, copy from:"
    if [[ -f "$PROJECT_DIR/$EVIDENCE_DIR/summary.txt" ]]; then
      echo "  $EVIDENCE_DIR/summary.txt"
    fi
    if [[ -f "$PROJECT_DIR/$EVIDENCE_DIR/sha256sums.txt" ]]; then
      echo "  $EVIDENCE_DIR/sha256sums.txt"
    fi
    echo ""
  fi
fi

# Next steps pointers.
echo "NEXT STEPS:"
case "$MODE" in
  local)
    echo "  - If wiring looks good, run staging: bash scripts/ops_fill_triage.sh staging"
    echo "  - Full triage: docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
    ;;
  staging)
    echo "  - Paste evidence block into PR body or incident notes"
    echo "  - If alert-input verification needed: bash scripts/ops_fill_triage.sh fire-drill"
    echo "  - Full triage: docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
    ;;
  fire-drill)
    echo "  - Paste summary.txt + sha256sums.txt into PR body or incident notes"
    echo "  - Full triage: docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
    ;;
esac
echo "  - Quickstart: docs/runbooks/00_OPS_QUICKSTART.md"
echo "  - Evidence index: docs/runbooks/00_EVIDENCE_INDEX.md"
echo ""

exit 0
