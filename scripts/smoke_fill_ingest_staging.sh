#!/usr/bin/env bash
# smoke_fill_ingest_staging.sh — Staging dry-run validation (Launch-06 PR4-PR7).
#
# Three gates + artifact assertion:
#   A: OFF (FakePort)       — enabled=0, no forbidden labels, fill metrics in artifact
#   B: ON  (real Binance)   — polls grow, cursor metrics present, no HTTP errors
#   C: Restart persistence  — cursor load OK, cursor doesn't regress (full-tuple monotonicity)
#   *: Every --metrics-out file is verified to contain grinder_fill_* lines (PR5)
#
# PR7: Persistent evidence artifacts saved under .artifacts/fill_ingest_staging/<ts>/
#       with sha256 + byte count for integrity verification.
#
# Gate A runs always (no API keys needed).
# Gates B+C require BINANCE_API_KEY + BINANCE_API_SECRET + ALLOW_MAINNET_TRADE=1.
# If credentials are missing, Gates B+C are SKIPPED (not failed).
#
# Safety: Gate B+C run with REMEDIATION_MODE=detect_only (dry_run=True).
# ALLOW_MAINNET_TRADE=1 is required for port initialization (even for reads),
# but detect_only ensures zero write ops (no place/cancel/replace).
#
# Usage:
#   bash scripts/smoke_fill_ingest_staging.sh
#
# Environment:
#   DUR_B       — Gate B duration in seconds (default: 75)
#   DUR_C       — Gate C duration in seconds (default: 30)
#   INTERVAL_MS — Reconcile interval in ms (default: 5000)
#
# Requirements:
#   - PYTHONPATH=src (auto-set by script)
#   - Python 3.11+
#   - For Gates B+C: BINANCE_API_KEY, BINANCE_API_SECRET, ALLOW_MAINNET_TRADE=1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

export PYTHONPATH=src

DUR_B="${DUR_B:-75}"
DUR_C="${DUR_C:-30}"
INTERVAL_MS="${INTERVAL_MS:-5000}"

# PR7: Persistent evidence directory (timestamped, gitignored).
EVIDENCE_TS="$(date +%Y%m%dT%H%M%S)"
EVIDENCE_DIR=".artifacts/fill_ingest_staging/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

# Centralized variable defaults — prevents set -u errors when Gates B+C are skipped.
PASS=0
FAIL=0
SKIP=0
GATE_A_SHA256="" GATE_A_BYTES="" GATE_A_FILL_COUNT=""
GATE_B_SHA256="" GATE_B_BYTES="" GATE_B_FILL_COUNT=""
GATE_C_SHA256="" GATE_C_BYTES="" GATE_C_FILL_COUNT=""
CURSOR_B_SHA256="" CURSOR_B_BYTES=""
CURSOR_C_SHA256="" CURSOR_C_BYTES=""
MONOTONICITY_RESULT="N/A"
HAS_CREDS=1

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }

# Extract a metric value from Prometheus text file.
# Usage: get_metric <file> <metric_with_labels>
# Example: get_metric /tmp/m.txt 'grinder_fill_ingest_polls_total{source="reconcile"}'
# Returns the numeric value (last field on the matching line), or "" if not found.
get_metric() {
  local file="$1" pattern="$2"
  grep -F "$pattern" "$file" 2>/dev/null | grep -v '^#' | awk '{print $NF}' | head -1
}

# Check that a metric value is a number > 0.
assert_positive() {
  local val="$1" msg="$2"
  if [[ -z "$val" ]]; then
    fail "$msg (metric not found)"
    return 1
  fi
  if python3 -c "import sys; sys.exit(0 if float('$val') > 0 else 1)" 2>/dev/null; then
    pass "$msg ($val > 0)"
    return 0
  else
    fail "$msg (got $val, want > 0)"
    return 1
  fi
}

# Check that a metric value equals expected.
assert_eq() {
  local val="$1" want="$2" msg="$3"
  if [[ -z "$val" ]]; then
    fail "$msg (metric not found)"
    return 1
  fi
  if python3 -c "import sys; sys.exit(0 if float('$val') == float('$want') else 1)" 2>/dev/null; then
    pass "$msg ($val == $want)"
    return 0
  else
    fail "$msg (got $val, want $want)"
    return 1
  fi
}

# Check no forbidden labels in fill metrics.
check_no_forbidden() {
  local file="$1" label="$2"
  if grep grinder_fill "$file" | grep -qE 'symbol=|order_id=|client_id=|trade_id='; then
    fail "forbidden labels in fill metrics ($label)"
    return 1
  else
    pass "no forbidden labels ($label)"
    return 0
  fi
}

# Assert that a --metrics-out artifact contains grinder_fill_* lines.
# This catches the PR3 bug where --metrics-out only wrote reconcile metrics.
assert_fill_metrics_in_artifact() {
  local file="$1" label="$2"
  if [[ ! -s "$file" ]]; then
    fail "metrics-out artifact empty or missing ($label)"
    return 1
  fi
  local count
  count=$(grep -c '^grinder_fill_' "$file" 2>/dev/null || echo "0")
  if [[ "$count" -gt 0 ]]; then
    pass "metrics-out artifact contains grinder_fill_* ($count lines, $label)"
    return 0
  else
    fail "metrics-out artifact missing grinder_fill_* lines ($label)"
    echo "    First 5 lines of artifact:"
    head -5 "$file" | sed 's/^/    /'
    return 1
  fi
}

# PR7: Save artifact with sha256 + byte count verification.
# Usage: save_artifact <src_path> <dest_name> <label>
# Sets globals: LAST_SHA256, LAST_BYTES (caller captures immediately).
save_artifact() {
  local src="$1" dest_name="$2" label="$3"
  if [[ ! -f "$src" ]]; then
    fail "artifact source missing ($label: $src)"
    LAST_SHA256=""
    LAST_BYTES=""
    return 1
  fi
  cp "$src" "$EVIDENCE_DIR/$dest_name"
  LAST_SHA256=$(sha256sum "$EVIDENCE_DIR/$dest_name" | awk '{print $1}')
  LAST_BYTES=$(wc -c < "$EVIDENCE_DIR/$dest_name")
  echo "  artifact: $dest_name  sha256=$LAST_SHA256  bytes=$LAST_BYTES"
  pass "artifact saved ($label: $dest_name)"
  return 0
}

# PR7: Validate cursor JSON has expected keys via print_fill_cursor.py.
# Usage: assert_json_valid <path>
assert_json_valid() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    fail "cursor file not found for JSON validation ($path)"
    return 1
  fi
  if python3 scripts/print_fill_cursor.py "$path" >/dev/null 2>&1; then
    pass "cursor file valid JSON with expected fields"
    return 0
  else
    fail "cursor file invalid or missing expected fields"
    return 1
  fi
}

# PR7: Full cursor monotonicity check — two separate assertions:
#   1. SSOT key (last_trade_id, last_ts_ms) must be non-decreasing (matches PR6 guard)
#   2. updated_at_ms must be non-decreasing
# Usage: assert_cursor_monotonic <before_path> <after_path>
assert_cursor_monotonic() {
  local before="$1" after="$2"
  local b_fields a_fields
  b_fields=$(python3 scripts/print_fill_cursor.py "$before" 2>/dev/null) || {
    fail "cannot read 'before' cursor for monotonicity ($before)"
    return 1
  }
  a_fields=$(python3 scripts/print_fill_cursor.py "$after" 2>/dev/null) || {
    fail "cannot read 'after' cursor for monotonicity ($after)"
    return 1
  }
  echo "  cursor before: $b_fields"
  echo "  cursor after:  $a_fields"

  # Check 1: SSOT key (trade_id, ts_ms) non-decreasing
  if python3 -c "
import sys
b = list(map(int, '${b_fields}'.split()))
a = list(map(int, '${a_fields}'.split()))
sys.exit(0 if (a[0], a[1]) >= (b[0], b[1]) else 1)
" 2>/dev/null; then
    pass "SSOT key monotonic ((trade_id, ts_ms): ($a_fields) >= ($b_fields))"
  else
    fail "SSOT key regressed ((trade_id, ts_ms): ($a_fields) < ($b_fields))"
    return 1
  fi

  # Check 2: updated_at_ms non-decreasing
  if python3 -c "
import sys
b = list(map(int, '${b_fields}'.split()))
a = list(map(int, '${a_fields}'.split()))
sys.exit(0 if a[2] >= b[2] else 1)
" 2>/dev/null; then
    pass "updated_at_ms monotonic"
  else
    fail "updated_at_ms regressed"
    return 1
  fi

  return 0
}

echo "=== Fill Ingest STAGING Smoke (Launch-06 PR4-PR7) ==="
echo "evidence_dir: $EVIDENCE_DIR"
echo ""

# =========================================================================
# Gate A: OFF (FakePort)
# =========================================================================

echo "--- Gate A: OFF (FakePort, no API keys) ---"

METRICS_A="/tmp/smoke_staging_gate_a_$$.txt"
rm -f "$METRICS_A"

FILL_INGEST_ENABLED=0 \
  python3 -m scripts.run_live_reconcile \
    --use-fake-port --duration 3 --metrics-port 0 \
    --metrics-out "$METRICS_A" >/dev/null 2>&1 || true

if [[ -f "$METRICS_A" ]]; then
  val=$(get_metric "$METRICS_A" 'grinder_fill_ingest_enabled{source="reconcile"}')
  assert_eq "$val" "0" "enabled gauge = 0 when OFF"
  check_no_forbidden "$METRICS_A" "Gate A"
  assert_fill_metrics_in_artifact "$METRICS_A" "Gate A"

  # PR7: Save artifact + capture evidence
  GATE_A_FILL_COUNT=$(grep -c '^grinder_fill_' "$METRICS_A" 2>/dev/null || echo "0")
  save_artifact "$METRICS_A" "gate_a_metrics.txt" "Gate A"
  GATE_A_SHA256="$LAST_SHA256"
  GATE_A_BYTES="$LAST_BYTES"
else
  fail "metrics file not created (Gate A)"
fi

rm -f "$METRICS_A"
echo ""

# =========================================================================
# Gate B: ON (real Binance reads, detect-only)
# =========================================================================

echo "--- Gate B: ON (real Binance reads, detect-only) ---"

# Check prerequisites for real port
if [[ -z "${BINANCE_API_KEY:-}" ]]; then
  echo "  BINANCE_API_KEY not set"
  HAS_CREDS=0
fi
if [[ -z "${BINANCE_API_SECRET:-}" ]]; then
  echo "  BINANCE_API_SECRET not set"
  HAS_CREDS=0
fi
if [[ "${ALLOW_MAINNET_TRADE:-0}" != "1" ]]; then
  echo "  ALLOW_MAINNET_TRADE not set to 1 (required for port init, safety via detect_only)"
  HAS_CREDS=0
fi

CURSOR_PATH="/tmp/smoke_staging_cursor_$$.json"
METRICS_B="/tmp/smoke_staging_gate_b_$$.txt"
METRICS_C="/tmp/smoke_staging_gate_c_$$.txt"

if [[ "$HAS_CREDS" -eq 0 ]]; then
  echo ""
  echo "  Skipping Gates B+C (set BINANCE_API_KEY, BINANCE_API_SECRET, ALLOW_MAINNET_TRADE=1)"
  skip "Gate B: real Binance reads (no credentials)"
  skip "Gate C: restart persistence (no credentials)"
  MONOTONICITY_RESULT="SKIP"
  echo ""
else
  rm -f "$CURSOR_PATH" "$METRICS_B" "$METRICS_C"

  echo "  Running reconcile (detect_only, duration=${DUR_B}s, interval=${INTERVAL_MS}ms)..."

  FILL_INGEST_ENABLED=1 \
  REMEDIATION_MODE=detect_only \
  FILL_CURSOR_PATH="$CURSOR_PATH" \
    python3 -m scripts.run_live_reconcile \
      --duration "$DUR_B" --interval-ms "$INTERVAL_MS" --metrics-port 0 \
      --metrics-out "$METRICS_B" >/dev/null 2>&1 || true

  if [[ -f "$METRICS_B" ]]; then
    # Enabled gauge = 1
    val=$(get_metric "$METRICS_B" 'grinder_fill_ingest_enabled{source="reconcile"}')
    assert_eq "$val" "1" "enabled gauge = 1 when ON"

    # Polls > 0
    val=$(get_metric "$METRICS_B" 'grinder_fill_ingest_polls_total{source="reconcile"}')
    assert_positive "$val" "polls_total > 0"

    # Cursor load present (ok or at least the HELP/TYPE line)
    if grep -q 'grinder_fill_cursor_load_total' "$METRICS_B"; then
      pass "cursor_load metric present"
    else
      fail "cursor_load metric missing"
    fi

    # Cursor save present
    if grep -q 'grinder_fill_cursor_save_total' "$METRICS_B"; then
      pass "cursor_save metric present"
    else
      fail "cursor_save metric missing"
    fi

    # PR6: Cursor stuck detection metrics present
    if grep -q 'grinder_fill_cursor_last_save_ts' "$METRICS_B"; then
      pass "cursor_last_save_ts metric present"
    else
      fail "cursor_last_save_ts metric missing"
    fi
    if grep -q 'grinder_fill_cursor_age_seconds' "$METRICS_B"; then
      pass "cursor_age_seconds metric present"
    else
      fail "cursor_age_seconds metric missing"
    fi

    # HTTP errors not exploding (0 or small)
    http_err=$(get_metric "$METRICS_B" 'grinder_fill_ingest_errors_total{source="reconcile",reason="http"}')
    if [[ -z "$http_err" || "$http_err" == "0" ]]; then
      pass "no HTTP errors"
    else
      if python3 -c "import sys; sys.exit(0 if float('$http_err') <= 3 else 1)" 2>/dev/null; then
        pass "HTTP errors within tolerance ($http_err <= 3)"
      else
        fail "HTTP errors too high ($http_err > 3)"
      fi
    fi

    # No forbidden labels
    check_no_forbidden "$METRICS_B" "Gate B"

    # Artifact assertion (PR5): fill metrics must be in --metrics-out
    assert_fill_metrics_in_artifact "$METRICS_B" "Gate B"

    # PR7: Save Gate B metrics artifact
    GATE_B_FILL_COUNT=$(grep -c '^grinder_fill_' "$METRICS_B" 2>/dev/null || echo "0")
    save_artifact "$METRICS_B" "gate_b_metrics.txt" "Gate B"
    GATE_B_SHA256="$LAST_SHA256"
    GATE_B_BYTES="$LAST_BYTES"
  else
    fail "metrics file not created (Gate B)"
  fi

  # Cursor file status after Run 1
  CURSOR_TRADE_ID_1=""
  if [[ -f "$CURSOR_PATH" ]]; then
    CURSOR_TRADE_ID_1=$(python3 -c "import json; d=json.load(open('$CURSOR_PATH')); print(d.get('last_trade_id', 0))" 2>/dev/null || echo "")
    echo "  cursor after Run 1: last_trade_id=$CURSOR_TRADE_ID_1"
    pass "cursor file exists after Run 1"

    # PR7: Save cursor snapshot + copy for Gate C monotonicity
    save_artifact "$CURSOR_PATH" "cursor_after_run1.json" "Gate B cursor"
    CURSOR_B_SHA256="$LAST_SHA256"
    CURSOR_B_BYTES="$LAST_BYTES"
    cp "$CURSOR_PATH" "$EVIDENCE_DIR/cursor_before_restart.json"
  else
    echo "  cursor file not created (quiet market -- no new fills, expected)"
    skip "cursor file not created (quiet market)"
  fi

  echo ""

  # =========================================================================
  # Gate C: Restart persistence
  # =========================================================================

  echo "--- Gate C: Restart persistence (duration=${DUR_C}s) ---"

  CURSOR_HASH_BEFORE=""
  if [[ -f "$CURSOR_PATH" ]]; then
    CURSOR_HASH_BEFORE=$(sha256sum "$CURSOR_PATH" | awk '{print $1}')
    echo "  cursor hash before restart: $CURSOR_HASH_BEFORE"
  fi

  echo "  Running reconcile again (same cursor path)..."

  FILL_INGEST_ENABLED=1 \
  REMEDIATION_MODE=detect_only \
  FILL_CURSOR_PATH="$CURSOR_PATH" \
    python3 -m scripts.run_live_reconcile \
      --duration "$DUR_C" --interval-ms "$INTERVAL_MS" --metrics-port 0 \
      --metrics-out "$METRICS_C" >/dev/null 2>&1 || true

  if [[ -f "$METRICS_C" ]]; then
    # Polls > 0 on Run 2 (loop is running)
    val=$(get_metric "$METRICS_C" 'grinder_fill_ingest_polls_total{source="reconcile"}')
    assert_positive "$val" "polls_total > 0 on Run 2"

    # Cursor load OK (if cursor file existed)
    if [[ -n "$CURSOR_TRADE_ID_1" ]]; then
      cl_ok=$(get_metric "$METRICS_C" 'grinder_fill_cursor_load_total{source="reconcile",result="ok"}')
      assert_positive "$cl_ok" "cursor_load{ok} > 0 on Run 2"
    fi

    # Cursor load error = 0
    cl_err=$(get_metric "$METRICS_C" 'grinder_fill_cursor_load_total{source="reconcile",result="error"}')
    if [[ -z "$cl_err" || "$cl_err" == "0" ]]; then
      pass "no cursor_load errors on Run 2"
    else
      fail "cursor_load errors on Run 2 ($cl_err)"
    fi

    # PR6: Cursor stuck detection metrics present after restart
    if grep -q 'grinder_fill_cursor_last_save_ts' "$METRICS_C"; then
      pass "cursor_last_save_ts present after restart"
    else
      fail "cursor_last_save_ts missing after restart"
    fi
    if grep -q 'grinder_fill_cursor_age_seconds' "$METRICS_C"; then
      pass "cursor_age_seconds present after restart"
    else
      fail "cursor_age_seconds missing after restart"
    fi

    # Artifact assertion (PR5)
    assert_fill_metrics_in_artifact "$METRICS_C" "Gate C"

    # PR7: Save Gate C metrics artifact
    GATE_C_FILL_COUNT=$(grep -c '^grinder_fill_' "$METRICS_C" 2>/dev/null || echo "0")
    save_artifact "$METRICS_C" "gate_c_metrics.txt" "Gate C"
    GATE_C_SHA256="$LAST_SHA256"
    GATE_C_BYTES="$LAST_BYTES"
  else
    fail "metrics file not created (Gate C)"
  fi

  # PR7: Cursor monotonicity check (full tuple, two separate assertions)
  if [[ -f "$CURSOR_PATH" ]]; then
    # Validate JSON with all expected fields
    assert_json_valid "$CURSOR_PATH"

    # Full monotonicity: SSOT key (trade_id, ts_ms) + updated_at_ms
    if [[ -f "$EVIDENCE_DIR/cursor_before_restart.json" ]]; then
      if assert_cursor_monotonic "$EVIDENCE_DIR/cursor_before_restart.json" "$CURSOR_PATH"; then
        MONOTONICITY_RESULT="PASS"
      else
        MONOTONICITY_RESULT="FAIL"
      fi
    elif [[ -n "$CURSOR_TRADE_ID_1" ]]; then
      fail "cursor_before_restart.json not found for monotonicity check"
      MONOTONICITY_RESULT="FAIL"
    else
      skip "cursor monotonicity (no cursor after Run 1)"
      MONOTONICITY_RESULT="SKIP"
    fi

    # PR7: Save Gate C cursor artifact
    save_artifact "$CURSOR_PATH" "cursor_after_run2.json" "Gate C cursor"
    CURSOR_C_SHA256="$LAST_SHA256"
    CURSOR_C_BYTES="$LAST_BYTES"
  elif [[ -z "$CURSOR_TRADE_ID_1" ]]; then
    # No cursor after either run -- quiet market
    skip "cursor monotonicity (quiet market -- no cursor file)"
    MONOTONICITY_RESULT="SKIP"
  else
    fail "cursor file disappeared after Run 2"
    MONOTONICITY_RESULT="FAIL"
  fi

  # Cleanup temp files (artifacts are persisted in EVIDENCE_DIR)
  rm -f "$METRICS_B" "$METRICS_C" "$CURSOR_PATH"
fi

echo ""

# =========================================================================
# Evidence block (PR7: dynamic with real data)
# =========================================================================

echo "=== EVIDENCE BLOCK (copy/paste into PR Proof) ==="
echo ""
echo "evidence_dir: $EVIDENCE_DIR"
echo ""
echo "Gate A: OFF (FakePort)"
echo "  enabled=0, no forbidden labels"
echo "  fill_line_count=$GATE_A_FILL_COUNT"
if [[ -n "$GATE_A_SHA256" ]]; then
  echo "  metrics: gate_a_metrics.txt  sha256=$GATE_A_SHA256  bytes=$GATE_A_BYTES"
fi
echo ""
if [[ "$HAS_CREDS" -eq 1 ]]; then
  echo "Gate B: ON (real Binance reads, detect_only)"
  if [[ -f "$EVIDENCE_DIR/gate_b_metrics.txt" ]]; then
    val_b=$(get_metric "$EVIDENCE_DIR/gate_b_metrics.txt" 'grinder_fill_ingest_enabled{source="reconcile"}')
    echo "  enabled=$val_b"
  fi
  echo "  fill_line_count=$GATE_B_FILL_COUNT"
  if [[ -n "$GATE_B_SHA256" ]]; then
    echo "  metrics: gate_b_metrics.txt  sha256=$GATE_B_SHA256  bytes=$GATE_B_BYTES"
  fi
  if [[ -n "$CURSOR_B_SHA256" ]]; then
    echo "  cursor: cursor_after_run1.json  sha256=$CURSOR_B_SHA256  bytes=$CURSOR_B_BYTES"
  fi
  echo ""
  echo "Gate C: Restart persistence"
  echo "  fill_line_count=$GATE_C_FILL_COUNT"
  if [[ -n "$GATE_C_SHA256" ]]; then
    echo "  metrics: gate_c_metrics.txt  sha256=$GATE_C_SHA256  bytes=$GATE_C_BYTES"
  fi
  if [[ -n "$CURSOR_C_SHA256" ]]; then
    echo "  cursor: cursor_after_run2.json  sha256=$CURSOR_C_SHA256  bytes=$CURSOR_C_BYTES"
  fi
  echo "  monotonicity=$MONOTONICITY_RESULT"
  echo ""
fi
echo "NOTE: Artifacts saved under $EVIDENCE_DIR (gitignored). Do not commit."
echo ""

echo "=== Results: $PASS passed, $FAIL failed, $SKIP skipped ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
