#!/usr/bin/env bash
# ops_risk_triage.sh -- One-command risk triage wrapper (Launch-08 PR3).
#
# Single entrypoint for all risk evidence scripts:
#   killswitch-drawdown -> fire_drill_risk_killswitch_drawdown.sh
#   budget-limits       -> fire_drill_reconcile_budget_limits.sh
#
# Does NOT invent a new evidence format -- runs the underlying script,
# surfaces its evidence_dir, and prints next-step pointers.
#
# Usage:
#   bash scripts/ops_risk_triage.sh <mode>
#   bash scripts/ops_risk_triage.sh -h|--help
#
# Modes:
#   killswitch-drawdown  Kill-switch latch + DrawdownGuardV1 enforcement (~2s).
#   budget-limits        BudgetTracker per-run + per-day notional caps (~2s).
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
  echo "  ops_risk_triage: $1"
  echo "========================================"
  echo ""
}

print_usage() {
  cat <<'USAGE'
Usage: bash scripts/ops_risk_triage.sh <mode> [options]

Modes:
  killswitch-drawdown  Kill-switch latch + DrawdownGuardV1 enforcement proof
  budget-limits        BudgetTracker per-run + per-day notional cap proof

Options:
  -h, --help       Show this help and exit
  --no-status      Skip git status guardrail check

Examples:
  bash scripts/ops_risk_triage.sh killswitch-drawdown
  bash scripts/ops_risk_triage.sh budget-limits
  bash scripts/ops_risk_triage.sh budget-limits --no-status

No API keys needed. No network calls. Both modes run ~2s.

Evidence artifacts are saved under .artifacts/... (gitignored). Do not commit.

Docs:
  docs/runbooks/00_OPS_QUICKSTART.md      -- Operator quickstart
  docs/runbooks/00_EVIDENCE_INDEX.md      -- Evidence artifact index
  docs/runbooks/04_KILL_SWITCH.md         -- Kill-switch runbook
  docs/runbooks/12_ACTIVE_REMEDIATION.md  -- Active remediation runbook
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
  die "No mode specified. Usage: bash scripts/ops_risk_triage.sh <mode> (try -h for help)"
fi

# =========================================================================
# Mode dispatch
# =========================================================================

case "$MODE" in
  killswitch-drawdown)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_risk_killswitch_drawdown.sh"
    LABEL="Kill-switch + DrawdownGuardV1 fire drill"
    TRIAGE_DOC="docs/runbooks/04_KILL_SWITCH.md"
    ;;
  budget-limits)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_reconcile_budget_limits.sh"
    LABEL="Budget/limits fire drill"
    TRIAGE_DOC="docs/runbooks/12_ACTIVE_REMEDIATION.md"
    ;;
  *)
    die "Unknown mode: '$MODE'. Valid modes: killswitch-drawdown, budget-limits (try -h for help)"
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
  echo "  docs/runbooks/00_OPS_QUICKSTART.md"
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
case "$MODE" in
  killswitch-drawdown)
    echo "  - Paste summary.txt + sha256sums.txt into PR body or incident notes"
    echo "  - Budget limits drill: bash scripts/ops_risk_triage.sh budget-limits"
    echo "  - Full triage: $TRIAGE_DOC"
    ;;
  budget-limits)
    echo "  - Paste summary.txt + sha256sums.txt into PR body or incident notes"
    echo "  - Kill-switch drill: bash scripts/ops_risk_triage.sh killswitch-drawdown"
    echo "  - Full triage: $TRIAGE_DOC"
    ;;
esac
echo "  - Quickstart: docs/runbooks/00_OPS_QUICKSTART.md"
echo "  - Evidence index: docs/runbooks/00_EVIDENCE_INDEX.md"
echo ""

exit 0
