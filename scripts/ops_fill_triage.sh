#!/usr/bin/env bash
# ops_fill_triage.sh -- Unified ops triage entrypoint (Launch-11 PR1).
#
# Single entrypoint for fill + connector evidence scripts:
#   local                  -> smoke_fill_ingest.sh
#   staging                -> smoke_fill_ingest_staging.sh
#   fire-drill             -> fire_drill_fill_alerts.sh
#   connector-market-data  -> fire_drill_connector_market_data.sh
#   connector-exchange-port -> fire_drill_connector_exchange_port.sh
#   sor-fire-drill         -> fire_drill_sor.sh
#   all                    -> runs connector-market-data + connector-exchange-port sequentially
#
# Does NOT invent a new evidence format -- runs the underlying script,
# surfaces its evidence_dir, and prints next-step pointers.
#
# Usage:
#   bash scripts/ops_fill_triage.sh <mode>
#   bash scripts/ops_fill_triage.sh -h|--help
#
# Modes:
#   local                  No API keys. Validates metric wiring via FakePort (~5s).
#   staging                Gate A always; B/C require BINANCE_API_KEY +
#                          BINANCE_API_SECRET + ALLOW_MAINNET_TRADE=1 (~2 min).
#   fire-drill             No API keys. Proves alert inputs are produced (~10s).
#   connector-market-data  No API keys. L2 parse, DQ, symbol whitelist (~2s).
#   connector-exchange-port No API keys. Gate chain, idempotency, retry (~2s).
#   sor-fire-drill         No API keys. SOR decision paths + metrics (~2s).
#   all                    Runs connector-market-data + connector-exchange-port (~4s).
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
  local                   Local smoke test (FakePort, no API keys needed)
  staging                 Staging smoke (Gate A always; B/C if creds set)
  fire-drill              Deterministic alert input proof (no API keys needed)
  connector-market-data   L2 parse, DQ staleness/gaps/outliers, symbol whitelist
  connector-exchange-port Gate chain, idempotency cache, retry classification
  sor-fire-drill          SOR decision paths (CANCEL_REPLACE/BLOCK/NOOP) + metrics
  all                     Run all connector modes sequentially (~4s)

Options:
  -h, --help       Show this help and exit
  --no-status      Skip git status guardrail check

Examples:
  bash scripts/ops_fill_triage.sh local
  bash scripts/ops_fill_triage.sh fire-drill
  bash scripts/ops_fill_triage.sh staging
  bash scripts/ops_fill_triage.sh connector-market-data
  bash scripts/ops_fill_triage.sh connector-exchange-port
  bash scripts/ops_fill_triage.sh sor-fire-drill
  bash scripts/ops_fill_triage.sh connector-market-data --no-status
  bash scripts/ops_fill_triage.sh all

No API keys needed for local, fire-drill, connector, or all modes.
Evidence artifacts are saved under .artifacts/... (gitignored). Do not commit.

Docs:
  docs/runbooks/00_OPS_QUICKSTART.md      -- Operator quickstart
  docs/runbooks/00_EVIDENCE_INDEX.md      -- Evidence artifact index
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
# Batch mode: "all" runs connector modes sequentially
# =========================================================================

if [[ "$MODE" == "all" ]]; then
  ALL_MODES=("connector-market-data" "connector-exchange-port")
  ALL_SCRIPTS=("fire_drill_connector_market_data.sh" "fire_drill_connector_exchange_port.sh")
  ALL_LABELS=("Market data connector fire drill" "Exchange port boundary fire drill")

  TOTAL=${#ALL_MODES[@]}
  PASS_COUNT=0
  FAIL_COUNT=0
  FAILED_MODES=()
  EVIDENCE_DIRS=()

  BATCH_START=$(date +%s)

  for i in "${!ALL_MODES[@]}"; do
    SUB_MODE="${ALL_MODES[$i]}"
    SUB_SCRIPT="$SCRIPT_DIR/${ALL_SCRIPTS[$i]}"
    SUB_LABEL="${ALL_LABELS[$i]}"

    print_header "$SUB_LABEL"

    SUB_START=$(date +%s)
    SUB_RC=0
    SUB_OUTPUT_FILE="$(mktemp)"

    bash "$SUB_SCRIPT" 2>&1 | tee "$SUB_OUTPUT_FILE" || SUB_RC=$?

    SUB_OUTPUT="$(cat "$SUB_OUTPUT_FILE")"
    rm -f "$SUB_OUTPUT_FILE"

    SUB_END=$(date +%s)
    SUB_ELAPSED=$((SUB_END - SUB_START))

    echo ""

    if [[ "$SUB_RC" -ne 0 ]]; then
      echo "========================================"
      echo "  FAILED: $SUB_MODE (exit code $SUB_RC)"
      echo "  elapsed: ${SUB_ELAPSED}s"
      echo "========================================"
      echo "EVIDENCE_REF mode=$SUB_MODE status=failed reason=exit_code_$SUB_RC"
      FAIL_COUNT=$((FAIL_COUNT + 1))
      FAILED_MODES+=("$SUB_MODE")
      EVIDENCE_DIRS+=("FAILED")
    else
      SUB_EVIDENCE="$(extract_evidence_dir "$SUB_OUTPUT")"
      echo "========================================"
      echo "  PASSED: $SUB_MODE"
      echo "  elapsed: ${SUB_ELAPSED}s"
      if [[ -n "$SUB_EVIDENCE" ]]; then
        echo "  evidence_dir: $SUB_EVIDENCE"
      fi
      echo "========================================"
      if [[ -n "$SUB_EVIDENCE" ]] && [[ "$SUB_EVIDENCE" != "UNKNOWN" ]]; then
        echo "EVIDENCE_REF mode=$SUB_MODE evidence_dir=$SUB_EVIDENCE summary=$SUB_EVIDENCE/summary.txt sha256sums=$SUB_EVIDENCE/sha256sums.txt"
      else
        echo "EVIDENCE_REF mode=$SUB_MODE status=passed evidence_dir=unknown"
      fi
      PASS_COUNT=$((PASS_COUNT + 1))
      EVIDENCE_DIRS+=("${SUB_EVIDENCE:-UNKNOWN}")
    fi
    echo ""
  done

  BATCH_END=$(date +%s)
  BATCH_ELAPSED=$((BATCH_END - BATCH_START))

  # Aggregated summary.
  echo "========================================"
  if [[ "$FAIL_COUNT" -eq 0 ]]; then
    echo "  ALL PASSED ($PASS_COUNT/$TOTAL)"
  else
    echo "  SOME FAILED ($PASS_COUNT passed, $FAIL_COUNT failed out of $TOTAL)"
    echo "  failed: ${FAILED_MODES[*]}"
  fi
  echo "  total elapsed: ${BATCH_ELAPSED}s"
  echo "========================================"
  echo ""

  # Per-mode evidence directories.
  for i in "${!ALL_MODES[@]}"; do
    if [[ "${EVIDENCE_DIRS[$i]}" != "FAILED" ]] && [[ "${EVIDENCE_DIRS[$i]}" != "UNKNOWN" ]]; then
      echo "  ${ALL_MODES[$i]}: ${EVIDENCE_DIRS[$i]}"
    fi
  done
  echo ""

  # Git status guardrail.
  if [[ "$SKIP_STATUS" -eq 0 ]]; then
    if git status --porcelain 2>/dev/null | grep -qF ".artifacts/"; then
      echo "WARNING: .artifacts/ appears in git status (gitignore may be broken)"
      git status --porcelain 2>/dev/null | grep -F ".artifacts/" | head -5
      echo ""
    fi
  fi

  echo "NEXT STEPS:"
  echo "  - Paste summary.txt + sha256sums.txt from each evidence_dir into PR body"
  echo "  - Quickstart: docs/runbooks/00_OPS_QUICKSTART.md"
  echo "  - Evidence index: docs/runbooks/00_EVIDENCE_INDEX.md"
  echo ""

  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
  fi
  exit 0
fi

# =========================================================================
# Mode dispatch (single mode)
# =========================================================================

case "$MODE" in
  local)
    SCRIPT_PATH="$SCRIPT_DIR/smoke_fill_ingest.sh"
    LABEL="Local smoke (FakePort)"
    HAS_ARTIFACTS=0
    TRIAGE_DOC="docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
    ;;
  staging)
    SCRIPT_PATH="$SCRIPT_DIR/smoke_fill_ingest_staging.sh"
    LABEL="Staging smoke (Gate A always; B/C if creds)"
    HAS_ARTIFACTS=1
    TRIAGE_DOC="docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
    ;;
  fire-drill)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_fill_alerts.sh"
    LABEL="Alert input fire drill"
    HAS_ARTIFACTS=1
    TRIAGE_DOC="docs/runbooks/26_FILL_TRACKER_TRIAGE.md"
    ;;
  connector-market-data)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_connector_market_data.sh"
    LABEL="Market data connector fire drill"
    HAS_ARTIFACTS=1
    TRIAGE_DOC="docs/runbooks/00_EVIDENCE_INDEX.md"
    ;;
  connector-exchange-port)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_connector_exchange_port.sh"
    LABEL="Exchange port boundary fire drill"
    HAS_ARTIFACTS=1
    TRIAGE_DOC="docs/runbooks/00_EVIDENCE_INDEX.md"
    ;;
  sor-fire-drill)
    SCRIPT_PATH="$SCRIPT_DIR/fire_drill_sor.sh"
    LABEL="SOR fire drill (Launch-14)"
    HAS_ARTIFACTS=1
    TRIAGE_DOC="docs/runbooks/28_SOR_FIRE_DRILL.md"
    ;;
  *)
    die "Unknown mode: '$MODE'. Valid modes: local, staging, fire-drill, connector-market-data, connector-exchange-port, sor-fire-drill, all (try -h for help)"
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
  # EVIDENCE_REF (machine-readable, always emitted).
  FAIL_EVIDENCE="$(extract_evidence_dir "$SCRIPT_OUTPUT")"
  if [[ -n "$FAIL_EVIDENCE" ]]; then
    echo "EVIDENCE_REF mode=$MODE status=failed reason=exit_code_$SCRIPT_RC evidence_dir=$FAIL_EVIDENCE"
  else
    echo "EVIDENCE_REF mode=$MODE status=failed reason=exit_code_$SCRIPT_RC"
  fi
  echo ""
  echo "NEXT: check the output above for FAIL lines, then see:"
  echo "  $TRIAGE_DOC"
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

# EVIDENCE_REF (machine-readable, always emitted).
if [[ "$HAS_ARTIFACTS" -eq 1 ]] && [[ -n "${EVIDENCE_DIR:-}" ]] && [[ "$EVIDENCE_DIR" != "UNKNOWN" ]]; then
  echo "EVIDENCE_REF mode=$MODE evidence_dir=$EVIDENCE_DIR summary=$EVIDENCE_DIR/summary.txt sha256sums=$EVIDENCE_DIR/sha256sums.txt"
elif [[ "$HAS_ARTIFACTS" -eq 1 ]]; then
  echo "EVIDENCE_REF mode=$MODE status=passed evidence_dir=unknown"
else
  echo "EVIDENCE_REF mode=$MODE status=passed"
fi
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
  connector-market-data)
    echo "  - Paste summary.txt + sha256sums.txt into PR body or incident notes"
    echo "  - Exchange port drill: bash scripts/ops_fill_triage.sh connector-exchange-port"
    echo "  - Full triage: $TRIAGE_DOC"
    ;;
  connector-exchange-port)
    echo "  - Paste summary.txt + sha256sums.txt into PR body or incident notes"
    echo "  - Market data drill: bash scripts/ops_fill_triage.sh connector-market-data"
    echo "  - Full triage: $TRIAGE_DOC"
    ;;
esac
echo "  - Quickstart: docs/runbooks/00_OPS_QUICKSTART.md"
echo "  - Evidence index: docs/runbooks/00_EVIDENCE_INDEX.md"
echo ""

exit 0
