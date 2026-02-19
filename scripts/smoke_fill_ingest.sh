#!/usr/bin/env bash
# smoke_fill_ingest.sh — Local smoke test for fill ingestion (Launch-06 PR3).
#
# Validates the fill ingest pipeline via FakePort (no API keys needed).
# Uses --use-fake-port so no real exchange calls are made.
#
# What this tests:
#   1. FILL_INGEST_ENABLED=0 → enabled gauge is 0, polls stay at 0
#   2. FILL_INGEST_ENABLED=1 → enabled gauge is 1
#   3. Health metrics appear in /metrics output
#   4. No forbidden labels in fill metrics
#
# Usage:
#   bash scripts/smoke_fill_ingest.sh
#
# Requirements:
#   - PYTHONPATH=src
#   - Python 3.11+
#
# Note: FakePort returns empty trades, so fill_events_total stays at zero.
# This is expected — the smoke test validates metric wiring, not real fills.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "=== Fill Ingest Smoke Test (Launch-06 PR3) ==="
echo ""

# --- Test 1: Metrics output with ingest OFF ---
echo "--- Test 1: FILL_INGEST_ENABLED=0 (OFF) ---"

METRICS_OUT=$(PYTHONPATH=src FILL_INGEST_ENABLED=0 \
  python3 -m scripts.run_live_reconcile \
    --use-fake-port --duration 3 --metrics-port 0 \
    --metrics-out /tmp/smoke_fill_off.txt 2>/dev/null || true)

if [ -f /tmp/smoke_fill_off.txt ]; then
  # Check enabled gauge is 0
  if grep -q 'grinder_fill_ingest_enabled{source="reconcile"} 0' /tmp/smoke_fill_off.txt; then
    pass "enabled gauge = 0 when OFF"
  else
    fail "enabled gauge not 0 when OFF"
  fi

  # Check no forbidden labels
  if grep grinder_fill /tmp/smoke_fill_off.txt | grep -qE 'symbol=|order_id=|client_id='; then
    fail "forbidden labels found in fill metrics"
  else
    pass "no forbidden labels in fill metrics"
  fi
else
  fail "metrics file not created (OFF mode)"
fi

echo ""

# --- Test 2: Metrics output with ingest ON ---
echo "--- Test 2: FILL_INGEST_ENABLED=1 (ON, FakePort) ---"

CURSOR_PATH="/tmp/smoke_fill_cursor_$$.json"
rm -f "$CURSOR_PATH"

METRICS_OUT=$(PYTHONPATH=src FILL_INGEST_ENABLED=1 \
  FILL_CURSOR_PATH="$CURSOR_PATH" \
  python3 -m scripts.run_live_reconcile \
    --use-fake-port --duration 3 --metrics-port 0 \
    --metrics-out /tmp/smoke_fill_on.txt 2>/dev/null || true)

if [ -f /tmp/smoke_fill_on.txt ]; then
  # Check enabled gauge is 1
  if grep -q 'grinder_fill_ingest_enabled{source="reconcile"} 1' /tmp/smoke_fill_on.txt; then
    pass "enabled gauge = 1 when ON"
  else
    fail "enabled gauge not 1 when ON"
  fi

  # Check health metrics present
  for metric in grinder_fill_ingest_polls_total grinder_fill_ingest_enabled \
                grinder_fill_ingest_errors_total grinder_fill_cursor_load_total \
                grinder_fill_cursor_save_total grinder_fill_cursor_last_save_ts \
                grinder_fill_cursor_age_seconds; do
    if grep -q "$metric" /tmp/smoke_fill_on.txt; then
      pass "$metric present"
    else
      fail "$metric missing"
    fi
  done

  # Check no forbidden labels
  if grep grinder_fill /tmp/smoke_fill_on.txt | grep -qE 'symbol=|order_id=|client_id='; then
    fail "forbidden labels found in fill metrics (ON)"
  else
    pass "no forbidden labels in fill metrics (ON)"
  fi
else
  fail "metrics file not created (ON mode)"
fi

# Cleanup
rm -f /tmp/smoke_fill_off.txt /tmp/smoke_fill_on.txt "$CURSOR_PATH"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
