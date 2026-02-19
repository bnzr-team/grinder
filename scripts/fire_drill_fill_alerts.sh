#!/usr/bin/env bash
# fire_drill_fill_alerts.sh — CI-safe fill alert fire drill (Launch-06 PR8).
#
# Deterministically triggers two fill alert scenarios:
#   Drill A: FillCursorNonMonotonicRejected — backward cursor write rejected
#   Drill B: FillCursorStuck — cursor save fails, age grows
#
# No API keys needed. No changes to src/grinder/. No real exchange calls.
# Takes ~10 seconds (6s of sleep in Drill B).
#
# Usage:
#   bash scripts/fire_drill_fill_alerts.sh
#
# Evidence artifacts saved under .artifacts/fill_alert_fire_drill/<ts>/
# (gitignored via .artifacts/ rule). Do not commit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

export PYTHONPATH=src

# =========================================================================
# Evidence directory
# =========================================================================

EVIDENCE_TS="$(date +%Y%m%dT%H%M%S)"
EVIDENCE_DIR=".artifacts/fill_alert_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== Fill Alert Fire Drill (Launch-06 PR8) ==="
echo "evidence_dir: ${EVIDENCE_DIR}"
echo ""

# =========================================================================
# Counters + helpers
# =========================================================================

PASS=0
FAIL=0
SKIP=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

bytes_file() {
  wc -c < "$1" | tr -d ' '
}

LAST_SHA256=""
LAST_BYTES=""

save_artifact() {
  local src="$1" name="$2" label="$3"
  if [[ ! -f "$src" ]]; then
    fail "artifact source missing ($label: $src)"
    LAST_SHA256=""
    LAST_BYTES=""
    return 1
  fi
  cp -f "$src" "$EVIDENCE_DIR/$name"
  LAST_SHA256="$(sha256_file "$EVIDENCE_DIR/$name")"
  LAST_BYTES="$(bytes_file "$EVIDENCE_DIR/$name")"
  echo "  artifact: ${name}  sha256=${LAST_SHA256}  bytes=${LAST_BYTES}"
  pass "artifact saved (${label}: ${name})"
}

assert_contains() {
  local file="$1" needle="$2" label="$3"
  if grep -qF "$needle" "$file" 2>/dev/null; then
    pass "$label"
  else
    fail "$label"
    echo "    expected: $needle"
    echo "    in file: $file"
  fi
}

# Extract full metric line by fixed-string prefix match.
get_metric_line() {
  local file="$1" prefix="$2"
  grep -F "$prefix" "$file" 2>/dev/null | grep -v '^#' | head -1 || true
}

# Extract numeric value (last field) from a Prometheus metric line.
metric_value() {
  awk '{print $NF}' <<< "$1"
}

# Generate sha256sums.txt (stable order, excludes itself).
write_sha256sums() {
  (
    cd "$EVIDENCE_DIR"
    find . -maxdepth 1 -type f ! -name 'sha256sums.txt' -print0 \
      | sort -z \
      | xargs -0 sha256sum
  ) > "$EVIDENCE_DIR/sha256sums.txt"
}

# =========================================================================
# Drill A: Non-monotonic rejection
# =========================================================================

echo "--- Drill A: Non-monotonic rejection ---"

CURSOR_A="$EVIDENCE_DIR/cursor_drill_a.json"

# Write HIGH tuple cursor to disk.
cat > "$CURSOR_A" <<'JSON'
{"last_trade_id": 9999, "last_ts_ms": 1700000000000, "updated_at_ms": 1700000000000}
JSON

# Snapshot cursor tuple BEFORE the save attempt.
CURSOR_BEFORE_A="$(python3 scripts/print_fill_cursor.py "$CURSOR_A")"
save_artifact "$CURSOR_A" "cursor_before_drill_a.json" "Drill A before"

# Attempt to save a LOW tuple. Expect: rejected, cursor unchanged.
DRILL_A_METRICS="$EVIDENCE_DIR/drill_a_metrics.txt"
DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"

PYTHONPATH=src python3 - "$CURSOR_A" "$DRILL_A_METRICS" <<PY 2>"$DRILL_A_LOG"
import logging
import sys

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from grinder.execution.fill_cursor import FillCursor, save_fill_cursor
from grinder.observability.fill_metrics import FillMetrics

cursor_path = sys.argv[1]
metrics_path = sys.argv[2]

fm = FillMetrics()
low_cursor = FillCursor(last_trade_id=100, last_ts_ms=1600000000000)
save_fill_cursor(cursor_path, low_cursor, now_ms=1600000000000, fill_metrics=fm, source="reconcile")

with open(metrics_path, "w") as f:
    f.write("\n".join(fm.to_prometheus_lines()) + "\n")
PY

# Snapshot cursor tuple AFTER the save attempt.
CURSOR_AFTER_A="$(python3 scripts/print_fill_cursor.py "$CURSOR_A")"
save_artifact "$CURSOR_A" "cursor_after_drill_a.json" "Drill A after"

# Assert: cursor tuple unchanged.
if [[ "$CURSOR_BEFORE_A" == "$CURSOR_AFTER_A" ]]; then
  pass "cursor tuple unchanged ($CURSOR_BEFORE_A == $CURSOR_AFTER_A)"
else
  fail "cursor tuple changed (before=$CURSOR_BEFORE_A after=$CURSOR_AFTER_A)"
fi

# Assert: rejected_non_monotonic counter in metrics.
assert_contains "$DRILL_A_METRICS" \
  'grinder_fill_cursor_save_total{source="reconcile",result="rejected_non_monotonic"}' \
  "rejected_non_monotonic counter in metrics"

# Assert: log marker present.
assert_contains "$DRILL_A_LOG" \
  "FILL_CURSOR_REJECTED_NON_MONOTONIC" \
  "FILL_CURSOR_REJECTED_NON_MONOTONIC in log output"

echo ""

# =========================================================================
# Drill B: Cursor stuck inputs (single Python process)
# =========================================================================

echo "--- Drill B: Cursor stuck inputs ---"

CURSOR_B="/tmp/fire_drill_cursor_b_$$.json"
DRILL_B_METRICS_1="$EVIDENCE_DIR/drill_b_metrics_1.txt"
DRILL_B_METRICS_2="$EVIDENCE_DIR/drill_b_metrics_2.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"

# Single Python process: successful save -> scrape 1 -> chmod -> failing saves -> scrape 2.
PYTHONPATH=src python3 - "$CURSOR_B" "$DRILL_B_METRICS_1" "$DRILL_B_METRICS_2" <<PY 2>"$DRILL_B_LOG"
import logging
import os
import sys
import time

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from grinder.execution.fill_cursor import FillCursor, save_fill_cursor
from grinder.observability.fill_metrics import FillMetrics

cursor_path = sys.argv[1]
metrics_1_path = sys.argv[2]
metrics_2_path = sys.argv[3]

fm = FillMetrics()

# Phase 1: One successful save (sets cursor_last_save_ts for source="reconcile").
cursor_ok = FillCursor(last_trade_id=500, last_ts_ms=1700000000000)
save_fill_cursor(cursor_path, cursor_ok, now_ms=1700000000000, fill_metrics=fm, source="reconcile")
fm.inc_ingest_polls("reconcile")

# Scrape 1: baseline with cursor_last_save_ts set.
with open(metrics_1_path, "w") as f:
    f.write("\n".join(fm.to_prometheus_lines()) + "\n")

# Phase 2: Make cursor file read-only, then attempt saves that must fail.
os.chmod(cursor_path, 0o400)
try:
    for i in range(3):
        time.sleep(2)
        fm.inc_ingest_polls("reconcile")
        # trade_id strictly increasing each iteration (501, 502, 503).
        # ts_ms non-decreasing. This ensures monotonicity guard passes
        # and failure is purely PermissionError on write_text().
        cursor_fail = FillCursor(last_trade_id=500 + i + 1, last_ts_ms=1700000000000)
        save_fill_cursor(
            cursor_path,
            cursor_fail,
            now_ms=1700000001000 + i * 1000,
            fill_metrics=fm,
            source="reconcile",
        )
finally:
    try:
        os.chmod(cursor_path, 0o644)
    except Exception:
        pass

# Scrape 2: after failed saves + time passage.
with open(metrics_2_path, "w") as f:
    f.write("\n".join(fm.to_prometheus_lines()) + "\n")
PY

save_artifact "$CURSOR_B" "cursor_drill_b.json" "Drill B cursor"
rm -f "$CURSOR_B"

# Extract metric lines from both scrapes.
LINE_POLLS_1="$(get_metric_line "$DRILL_B_METRICS_1" 'grinder_fill_ingest_polls_total{source="reconcile"}')"
LINE_OK_1="$(get_metric_line "$DRILL_B_METRICS_1" 'grinder_fill_cursor_save_total{source="reconcile",result="ok"}')"
LINE_AGE_1="$(get_metric_line "$DRILL_B_METRICS_1" 'grinder_fill_cursor_age_seconds{source="reconcile"}')"

LINE_POLLS_2="$(get_metric_line "$DRILL_B_METRICS_2" 'grinder_fill_ingest_polls_total{source="reconcile"}')"
LINE_ERR_2="$(get_metric_line "$DRILL_B_METRICS_2" 'grinder_fill_cursor_save_total{source="reconcile",result="error"}')"
LINE_AGE_2="$(get_metric_line "$DRILL_B_METRICS_2" 'grinder_fill_cursor_age_seconds{source="reconcile"}')"

# Assert metric lines exist.
if [[ -n "$LINE_POLLS_1" ]]; then pass "polls_total present (scrape 1)"; else fail "polls_total missing (scrape 1)"; fi
if [[ -n "$LINE_OK_1" ]]; then pass "cursor_save ok present (scrape 1)"; else fail "cursor_save ok missing (scrape 1)"; fi
if [[ -n "$LINE_AGE_1" ]]; then pass "cursor_age_seconds present (scrape 1)"; else fail "cursor_age_seconds missing (scrape 1)"; fi
if [[ -n "$LINE_POLLS_2" ]]; then pass "polls_total present (scrape 2)"; else fail "polls_total missing (scrape 2)"; fi
if [[ -n "$LINE_ERR_2" ]]; then pass "cursor_save error present (scrape 2)"; else fail "cursor_save error missing (scrape 2)"; fi
if [[ -n "$LINE_AGE_2" ]]; then pass "cursor_age_seconds present (scrape 2)"; else fail "cursor_age_seconds missing (scrape 2)"; fi

# Numeric assertions.
POLLS_1="$(metric_value "$LINE_POLLS_1")"
POLLS_2="$(metric_value "$LINE_POLLS_2")"
OK_1="$(metric_value "$LINE_OK_1")"
ERR_2="$(metric_value "$LINE_ERR_2")"
AGE_1="$(metric_value "$LINE_AGE_1")"
AGE_2="$(metric_value "$LINE_AGE_2")"

if awk "BEGIN { exit !($POLLS_1 > 0) }" 2>/dev/null; then
  pass "polls_total > 0 (scrape 1: $POLLS_1)"
else
  fail "polls_total not > 0 (scrape 1: $POLLS_1)"
fi

if awk "BEGIN { exit !($POLLS_2 > 0) }" 2>/dev/null; then
  pass "polls_total > 0 (scrape 2: $POLLS_2)"
else
  fail "polls_total not > 0 (scrape 2: $POLLS_2)"
fi

if awk "BEGIN { exit !($OK_1 > 0) }" 2>/dev/null; then
  pass "cursor_save ok > 0 (scrape 1: $OK_1)"
else
  fail "cursor_save ok not > 0 (scrape 1: $OK_1)"
fi

if awk "BEGIN { exit !($ERR_2 > 0) }" 2>/dev/null; then
  pass "cursor_save error > 0 (scrape 2: $ERR_2)"
else
  fail "cursor_save error not > 0 (scrape 2: $ERR_2)"
fi

# Age must have grown by > 2.0 seconds between scrapes.
if awk "BEGIN { exit !($AGE_2 > $AGE_1 + 2.0) }" 2>/dev/null; then
  pass "cursor_age_seconds grew (${AGE_1} -> ${AGE_2}, delta > 2.0)"
else
  fail "cursor_age_seconds did not grow enough (${AGE_1} -> ${AGE_2}, want delta > 2.0)"
  echo "    line_age_1: ${LINE_AGE_1}"
  echo "    line_age_2: ${LINE_AGE_2}"
fi

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

# Exact metric lines for copy/paste proof.
DRILL_A_REJECTED_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_fill_cursor_save_total{source="reconcile",result="rejected_non_monotonic"}')"
DRILL_A_LOG_COUNT="$(grep -c 'FILL_CURSOR_REJECTED_NON_MONOTONIC' "$DRILL_A_LOG" 2>/dev/null || echo 0)"

{
  echo "Fill Alert Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: Non-monotonic rejection"
  echo "  cursor_before: ${CURSOR_BEFORE_A}"
  echo "  cursor_after:  ${CURSOR_AFTER_A}"
  echo "  metric: ${DRILL_A_REJECTED_LINE}"
  echo "  log_marker_count: ${DRILL_A_LOG_COUNT}"
  echo ""
  echo "Drill B: Cursor stuck inputs"
  echo "  scrape 1:"
  echo "    ${LINE_POLLS_1}"
  echo "    ${LINE_OK_1}"
  echo "    ${LINE_AGE_1}"
  echo "  scrape 2:"
  echo "    ${LINE_POLLS_2}"
  echo "    ${LINE_ERR_2}"
  echo "    ${LINE_AGE_2}"
  echo "  parsed: polls1=${POLLS_1} polls2=${POLLS_2} ok1=${OK_1} err2=${ERR_2} age1=${AGE_1} age2=${AGE_2}"
  echo ""
  echo "NOTE: Artifacts saved under ${EVIDENCE_DIR} (gitignored). Do not commit."
} > "$EVIDENCE_DIR/summary.txt"

# sha256sums (stable order, full hashes, excludes itself).
write_sha256sums

# =========================================================================
# Git guardrail
# =========================================================================

if git status --porcelain 2>/dev/null | grep -qF ".artifacts/"; then
  fail ".artifacts/ appears in git status --porcelain (gitignore broken)"
  git status --porcelain 2>/dev/null | grep -F ".artifacts/" | head -5
else
  pass ".artifacts/ not present in git status (gitignored)"
fi

# =========================================================================
# Final output
# =========================================================================

echo ""
echo "=== EVIDENCE BLOCK (copy/paste into PR proof) ==="
echo ""
cat "$EVIDENCE_DIR/summary.txt"
echo ""
echo "sha256sums:"
cat "$EVIDENCE_DIR/sha256sums.txt"
echo ""
echo "NOTE: Artifacts saved under .artifacts/... (gitignored). Do not commit."
echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
