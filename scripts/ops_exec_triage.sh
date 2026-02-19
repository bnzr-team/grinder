#!/usr/bin/env bash
# ops_exec_triage.sh -- One-command execution triage wrapper (Launch-09 PR2).
#
# Single entrypoint for execution evidence scripts:
#   exec-fire-drill -> fire_drill_execution_intents.sh
#
# Does NOT invent a new evidence format -- runs the underlying script,
# surfaces its evidence_dir, and prints next-step pointers.
#
# Usage:
#   bash scripts/ops_exec_triage.sh <mode>
#   bash scripts/ops_exec_triage.sh -h|--help
#
# Modes:
#   exec-fire-drill  Execution intent gate chain proof (~2s).
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
  echo "  ops_exec_triage: $1"
  echo "========================================"
  echo ""
}

print_usage() {
  cat <<'USAGE'
Usage: bash scripts/ops_exec_triage.sh <mode> [options]

Modes:
  exec-fire-drill  Execution intent gate chain proof (NOT_ARMED, kill-switch,
                   drawdown, all-gates-pass)

Options:
  -h, --help       Show this help and exit
  --no-status      Skip git status guardrail check

Examples:
  bash scripts/ops_exec_triage.sh exec-fire-drill
  bash scripts/ops_exec_triage.sh exec-fire-drill --no-status

No API keys needed. No network calls. Runs ~2s.

Evidence artifacts are saved under .artifacts/... (gitignored). Do not commit.

Docs:
  docs/runbooks/00_OPS_QUICKSTART.md      -- Operator quickstart
  docs/runbooks/00_EVIDENCE_INDEX.md      -- Evidence artifact index
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
  die "No mode specified. Usage: bash scripts/ops_exec_triage.sh <mode> (try -h for help)"
fi

# =========================================================================
# Mode dispatch
# =========================================================================

case "$MODE" in
  exec-fire-drill)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_execution_intents.sh"
    LABEL="Execution intent gate chain fire drill"
    TRIAGE_DOC="docs/runbooks/00_OPS_QUICKSTART.md"
    ;;
  *)
    die "Unknown mode: '$MODE'. Valid modes: exec-fire-drill (try -h for help)"
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
  echo "  $TRIAGE_DOC"
  echo "  docs/runbooks/00_EVIDENCE_INDEX.md"
  exit 1
fi

echo "========================================"
echo "  PASSED: $MODE"
echo "  elapsed: ${ELAPSED}s"

# Surface evidence directory.
EVIDENCE_DIR="$(extract_evidence_dir "$SCRIPT_OUTPUT")"
if [[ -n "$EVIDENCE_DIR" ]]; then
  echo "  evidence_dir: $EVIDENCE_DIR"
else
  echo "  evidence_dir: UNKNOWN"
fi

echo "========================================"
echo ""

# Git status guardrail.
if [[ "$SKIP_STATUS" -eq 0 ]]; then
  if git status --porcelain 2>/dev/null | grep -qF ".artifacts/"; then
    echo "WARNING: .artifacts/ appears in git status (gitignore may be broken)"
    git status --porcelain 2>/dev/null | grep -F ".artifacts/" | head -5
    echo ""
  fi
fi

# Artifact reminder.
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

# Next steps pointers.
echo "NEXT STEPS:"
echo "  - Paste summary.txt + sha256sums.txt into PR body or incident notes"
echo "  - Risk drills: bash scripts/ops_risk_triage.sh killswitch-drawdown"
echo "  - Fill drills: bash scripts/ops_fill_triage.sh local"
echo "  - Quickstart: docs/runbooks/00_OPS_QUICKSTART.md"
echo "  - Evidence index: docs/runbooks/00_EVIDENCE_INDEX.md"
echo ""

exit 0
