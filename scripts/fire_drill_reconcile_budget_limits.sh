#!/usr/bin/env bash
# fire_drill_reconcile_budget_limits.sh -- CI-safe budget/limits fire drill (Launch-08 PR2).
#
# Deterministically exercises two budget enforcement paths:
#   Drill A: Per-run notional cap blocks execution
#   Drill B: Per-day notional cap blocks execution (cumulative across calls)
#
# Both drills prove:
#   1. BudgetTracker.can_execute() returns the correct block reason
#   2. The block reason maps to the correct RemediationBlockReason enum
#   3. ReconcileMetrics renders the blocked action in Prometheus text
#   4. Budget state persistence works (state file written/read correctly)
#
# No API keys needed. No network calls. No changes to src/grinder/.
# Takes ~2 seconds (pure CPU, no sleeps).
#
# Usage:
#   bash scripts/fire_drill_reconcile_budget_limits.sh
#
# Evidence artifacts saved under .artifacts/budget_fire_drill/<ts>/
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
EVIDENCE_DIR=".artifacts/budget_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== Budget/Limits Fire Drill (Launch-08 PR2) ==="
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

get_metric_line() {
  local file="$1" prefix="$2"
  grep -F "$prefix" "$file" 2>/dev/null | grep -v '^#' | head -1 || true
}

metric_value() {
  awk '{print $NF}' <<< "$1"
}

write_sha256sums() {
  local sha_cmd="sha256sum"
  if ! command -v sha256sum >/dev/null 2>&1; then
    sha_cmd="shasum -a 256"
  fi
  (
    cd "$EVIDENCE_DIR"
    find . -maxdepth 1 -type f ! -name 'sha256sums.txt' -print0 \
      | sort -z \
      | xargs -0 $sha_cmd
  ) > "$EVIDENCE_DIR/sha256sums.txt"
}

# =========================================================================
# Drill A: Per-run notional cap blocks execution
# =========================================================================

echo "--- Drill A: Per-run notional cap ---"

DRILL_A_METRICS="$EVIDENCE_DIR/drill_a_metrics.txt"
DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"
DRILL_A_STATE="$EVIDENCE_DIR/drill_a_state.json"

PYTHONPATH=src python3 - "$DRILL_A_METRICS" "$DRILL_A_STATE" <<'PY' 2>"$DRILL_A_LOG"
import json
import logging
import sys
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

from grinder.reconcile.budget import BudgetTracker
from grinder.reconcile.remediation import RemediationBlockReason
from grinder.reconcile.metrics import (
    ReconcileMetrics,
    get_reconcile_metrics,
    reset_reconcile_metrics,
)
from grinder.observability.metrics_builder import MetricsBuilder

metrics_path = sys.argv[1]
state_path = sys.argv[2]

# --- Phase 1: Create tracker with tiny per-run notional cap ---
tracker = BudgetTracker(
    max_calls_per_day=100,
    max_notional_per_day=Decimal("50000"),
    max_calls_per_run=100,
    max_notional_per_run=Decimal("500"),  # $500 per-run cap
    state_path=state_path,
)

print(f"budget_max_notional_per_run: {tracker.max_notional_per_run}", file=sys.stderr)
print(f"budget_max_notional_per_day: {tracker.max_notional_per_day}", file=sys.stderr)

# --- Phase 2: First check -- within budget ---
can_exec, reason = tracker.can_execute(Decimal("200"))
assert can_exec, f"$200 should be within $500 per-run cap, got blocked: {reason}"
print(f"check_200: can_execute={can_exec} reason={reason}", file=sys.stderr)

# Record the $200 execution
tracker.record_execution(Decimal("200"))
print(f"recorded_200: notional_this_run={tracker._state.notional_this_run}", file=sys.stderr)

# --- Phase 3: Second check -- still within budget ---
can_exec2, reason2 = tracker.can_execute(Decimal("200"))
assert can_exec2, f"$400 cumulative should be within $500 cap, got blocked: {reason2}"
print(f"check_400_cumulative: can_execute={can_exec2} reason={reason2}", file=sys.stderr)

# Record the second $200 execution
tracker.record_execution(Decimal("200"))
print(f"recorded_400: notional_this_run={tracker._state.notional_this_run}", file=sys.stderr)

# --- Phase 4: Third check -- EXCEEDS per-run cap ---
# $400 used + $200 attempted = $600 > $500 cap
can_exec3, reason3 = tracker.can_execute(Decimal("200"))
assert not can_exec3, "Third $200 should be blocked (would exceed $500 per-run cap)"
assert reason3 == "max_notional_per_run", f"Expected max_notional_per_run, got {reason3}"
print(f"check_600_cumulative: can_execute={can_exec3} reason={reason3}", file=sys.stderr)

# Verify the notional values used in the block decision
attempted_total = tracker._state.notional_this_run + Decimal("200")
print(f"block_decision_values: used={tracker._state.notional_this_run} attempted=200 total_would_be={attempted_total} cap={tracker.max_notional_per_run}", file=sys.stderr)

# --- Phase 5: Map to RemediationBlockReason enum ---
reason_map = {
    "max_calls_per_run": RemediationBlockReason.MAX_CALLS_PER_RUN,
    "max_notional_per_run": RemediationBlockReason.MAX_NOTIONAL_PER_RUN,
    "max_calls_per_day": RemediationBlockReason.MAX_CALLS_PER_DAY,
    "max_notional_per_day": RemediationBlockReason.MAX_NOTIONAL_PER_DAY,
}
mapped_reason = reason_map[reason3]
assert mapped_reason == RemediationBlockReason.MAX_NOTIONAL_PER_RUN
print(f"mapped_block_reason: {mapped_reason.value}", file=sys.stderr)

# --- Phase 6: Metrics proof ---
reset_reconcile_metrics()
m = get_reconcile_metrics()

# Record the block in metrics (same path as remediation.py:566)
m.record_action_blocked(mapped_reason.value)

# Set budget metrics (same path as remediation.py sync_budget_metrics)
used = tracker.get_used()
remaining = tracker.get_remaining()
m.set_budget_metrics(
    calls_used=used["calls_used_day"],
    notional_used=Decimal(str(used["notional_used_day"])),
    calls_remaining=remaining["calls_remaining_day"],
    notional_remaining=Decimal(str(remaining["notional_remaining_day"])),
    configured=True,
)

print(f"metrics_budget_calls_used_day: {used['calls_used_day']}", file=sys.stderr)
print(f"metrics_budget_notional_used_day: {used['notional_used_day']}", file=sys.stderr)
print(f"metrics_budget_calls_remaining_day: {remaining['calls_remaining_day']}", file=sys.stderr)
print(f"metrics_budget_notional_remaining_day: {remaining['notional_remaining_day']}", file=sys.stderr)

builder = MetricsBuilder()
output = builder.build()

with open(metrics_path, "w") as f:
    f.write(output)

print("metrics_written: drill_a_metrics.txt", file=sys.stderr)

# --- Phase 7: State persistence proof ---
# State file was written by record_execution() calls
import json as json_mod
state_data = json_mod.loads(open(state_path).read())
print(f"state_file_date: {state_data['date']}", file=sys.stderr)
print(f"state_file_calls_today: {state_data['calls_today']}", file=sys.stderr)
print(f"state_file_notional_today: {state_data['notional_today']}", file=sys.stderr)
print(f"state_file_last_updated_ts_ms: {state_data['last_updated_ts_ms']}", file=sys.stderr)

# Cleanup global state
reset_reconcile_metrics()
PY

# Assert block reason in log
assert_contains "$DRILL_A_LOG" \
  "check_600_cumulative: can_execute=False reason=max_notional_per_run" \
  "log: per-run notional cap blocks third execution"

assert_contains "$DRILL_A_LOG" \
  "mapped_block_reason: max_notional_per_run" \
  "log: block reason maps to RemediationBlockReason enum"

# Assert block decision shows attempted notional
assert_contains "$DRILL_A_LOG" \
  "block_decision_values: used=400 attempted=200 total_would_be=600 cap=500" \
  "log: block evaluated on attempted notional (used + new)"

# Assert metrics contain blocked action counter
assert_contains "$DRILL_A_METRICS" \
  'grinder_reconcile_action_blocked_total{reason="max_notional_per_run"} 1' \
  "metric: action_blocked_total{reason=max_notional_per_run} = 1"

# Assert budget metrics rendered
assert_contains "$DRILL_A_METRICS" \
  "grinder_reconcile_budget_calls_used_day 2" \
  "metric: budget_calls_used_day = 2"

assert_contains "$DRILL_A_METRICS" \
  "grinder_reconcile_budget_notional_used_day 400.00" \
  "metric: budget_notional_used_day = 400.00"

assert_contains "$DRILL_A_METRICS" \
  "grinder_reconcile_budget_configured 1" \
  "metric: budget_configured = 1"

# Assert state file written
if [[ -f "$DRILL_A_STATE" ]]; then
  pass "state file exists: drill_a_state.json"
else
  fail "state file missing: drill_a_state.json"
fi

assert_contains "$DRILL_A_LOG" \
  "state_file_calls_today: 2" \
  "log: state persistence records 2 calls"

echo ""

# =========================================================================
# Drill B: Per-day notional cap blocks execution (cumulative)
# =========================================================================

echo "--- Drill B: Per-day notional cap (cumulative across calls) ---"

DRILL_B_METRICS="$EVIDENCE_DIR/drill_b_metrics.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"
DRILL_B_STATE="$EVIDENCE_DIR/drill_b_state.json"

PYTHONPATH=src python3 - "$DRILL_B_METRICS" "$DRILL_B_STATE" <<'PY' 2>"$DRILL_B_LOG"
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

from grinder.reconcile.budget import BudgetTracker, BudgetState
from grinder.reconcile.remediation import RemediationBlockReason
from grinder.reconcile.metrics import (
    ReconcileMetrics,
    get_reconcile_metrics,
    reset_reconcile_metrics,
)
from grinder.observability.metrics_builder import MetricsBuilder

metrics_path = sys.argv[1]
state_path = sys.argv[2]

# --- Phase 1: Create tracker with generous per-run but tiny per-day cap ---
tracker = BudgetTracker(
    max_calls_per_day=100,
    max_notional_per_day=Decimal("1000"),  # $1000 per-day cap
    max_calls_per_run=100,
    max_notional_per_run=Decimal("50000"),  # per-run is NOT the bottleneck
    state_path=state_path,
)

print(f"budget_max_notional_per_day: {tracker.max_notional_per_day}", file=sys.stderr)
print(f"budget_max_notional_per_run: {tracker.max_notional_per_run}", file=sys.stderr)

# --- Phase 2: Prove per-day window is keyed by UTC date ---
today_str = datetime.now(UTC).strftime("%Y-%m-%d")
internal_date = tracker._state.date
print(f"day_key_utc: {today_str}", file=sys.stderr)
print(f"day_key_internal: {internal_date}", file=sys.stderr)
assert internal_date == today_str, f"Expected UTC date {today_str}, got {internal_date}"
print(f"day_key_match: True (both UTC {today_str})", file=sys.stderr)

# --- Phase 3: First execution -- within daily budget ---
can_exec, reason = tracker.can_execute(Decimal("600"))
assert can_exec, f"$600 should be within $1000 per-day cap, got blocked: {reason}"
tracker.record_execution(Decimal("600"))
print(f"recorded_600: notional_today={tracker._state.notional_today}", file=sys.stderr)

# --- Phase 4: Reset run counters (simulates new reconcile run) ---
tracker.reset_run_counters()
print(f"after_run_reset: notional_this_run={tracker._state.notional_this_run} notional_today={tracker._state.notional_today}", file=sys.stderr)

# Verify per-run counter reset but per-day persists
assert tracker._state.notional_this_run == Decimal("0"), "per-run should reset"
assert tracker._state.notional_today == Decimal("600"), "per-day should persist"

# --- Phase 5: Second execution in new run -- EXCEEDS per-day cap ---
# $600 used today + $500 attempted = $1100 > $1000 daily cap
can_exec2, reason2 = tracker.can_execute(Decimal("500"))
assert not can_exec2, "Second run $500 should be blocked (daily total would be $1100 > $1000)"
assert reason2 == "max_notional_per_day", f"Expected max_notional_per_day, got {reason2}"
print(f"cross_run_block: can_execute={can_exec2} reason={reason2}", file=sys.stderr)

# Show the exact values in the block decision
print(f"block_decision_values: notional_today={tracker._state.notional_today} attempted=500 total_would_be={tracker._state.notional_today + Decimal('500')} daily_cap={tracker.max_notional_per_day}", file=sys.stderr)

# --- Phase 6: Prove a smaller amount still works ---
can_exec3, reason3 = tracker.can_execute(Decimal("300"))
assert can_exec3, f"$300 should be within remaining $400 daily budget, got blocked: {reason3}"
print(f"smaller_amount: can_execute={can_exec3} reason={reason3} (within remaining budget)", file=sys.stderr)

# --- Phase 7: Map to RemediationBlockReason enum ---
reason_map = {
    "max_calls_per_run": RemediationBlockReason.MAX_CALLS_PER_RUN,
    "max_notional_per_run": RemediationBlockReason.MAX_NOTIONAL_PER_RUN,
    "max_calls_per_day": RemediationBlockReason.MAX_CALLS_PER_DAY,
    "max_notional_per_day": RemediationBlockReason.MAX_NOTIONAL_PER_DAY,
}
mapped_reason = reason_map[reason2]
assert mapped_reason == RemediationBlockReason.MAX_NOTIONAL_PER_DAY
print(f"mapped_block_reason: {mapped_reason.value}", file=sys.stderr)

# --- Phase 8: Metrics proof ---
reset_reconcile_metrics()
m = get_reconcile_metrics()

# Record the block
m.record_action_blocked(mapped_reason.value)

# Set budget metrics
used = tracker.get_used()
remaining = tracker.get_remaining()
m.set_budget_metrics(
    calls_used=used["calls_used_day"],
    notional_used=Decimal(str(used["notional_used_day"])),
    calls_remaining=remaining["calls_remaining_day"],
    notional_remaining=Decimal(str(remaining["notional_remaining_day"])),
    configured=True,
)

print(f"metrics_budget_notional_used_day: {used['notional_used_day']}", file=sys.stderr)
print(f"metrics_budget_notional_remaining_day: {remaining['notional_remaining_day']}", file=sys.stderr)

builder = MetricsBuilder()
output = builder.build()

with open(metrics_path, "w") as f:
    f.write(output)

print("metrics_written: drill_b_metrics.txt", file=sys.stderr)

# --- Phase 9: State persistence proof (across runs) ---
state_data = json.loads(open(state_path).read())
print(f"state_file_date: {state_data['date']}", file=sys.stderr)
print(f"state_file_calls_today: {state_data['calls_today']}", file=sys.stderr)
print(f"state_file_notional_today: {state_data['notional_today']}", file=sys.stderr)

# Cleanup global state
reset_reconcile_metrics()
PY

# Assert cross-run blocking
assert_contains "$DRILL_B_LOG" \
  "cross_run_block: can_execute=False reason=max_notional_per_day" \
  "log: per-day notional cap blocks across run boundary"

assert_contains "$DRILL_B_LOG" \
  "mapped_block_reason: max_notional_per_day" \
  "log: block reason maps to RemediationBlockReason enum"

# Assert block decision values (proves evaluated on attempted notional)
assert_contains "$DRILL_B_LOG" \
  "block_decision_values: notional_today=600 attempted=500 total_would_be=1100 daily_cap=1000" \
  "log: block evaluated on attempted notional (today + new)"

# Assert per-day window is UTC-keyed
assert_contains "$DRILL_B_LOG" \
  "day_key_match: True" \
  "log: per-day window keyed by UTC date"

# Assert smaller amount still works after block
assert_contains "$DRILL_B_LOG" \
  "smaller_amount: can_execute=True" \
  "log: amount within remaining daily budget still allowed"

# Assert run reset preserves daily state
assert_contains "$DRILL_B_LOG" \
  "after_run_reset: notional_this_run=0 notional_today=600" \
  "log: run reset clears per-run, preserves per-day"

# Assert metrics
assert_contains "$DRILL_B_METRICS" \
  'grinder_reconcile_action_blocked_total{reason="max_notional_per_day"} 1' \
  "metric: action_blocked_total{reason=max_notional_per_day} = 1"

assert_contains "$DRILL_B_METRICS" \
  "grinder_reconcile_budget_notional_used_day 600.00" \
  "metric: budget_notional_used_day = 600.00"

assert_contains "$DRILL_B_METRICS" \
  "grinder_reconcile_budget_configured 1" \
  "metric: budget_configured = 1"

# Assert state persistence
if [[ -f "$DRILL_B_STATE" ]]; then
  pass "state file exists: drill_b_state.json"
else
  fail "state file missing: drill_b_state.json"
fi

assert_contains "$DRILL_B_LOG" \
  "state_file_notional_today: 600" \
  "log: state file persists daily notional across runs"

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

DRILL_A_BLOCKED_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_reconcile_action_blocked_total{reason="max_notional_per_run"}')"
DRILL_A_BUDGET_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_reconcile_budget_notional_used_day')"
DRILL_B_BLOCKED_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_reconcile_action_blocked_total{reason="max_notional_per_day"}')"
DRILL_B_BUDGET_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_reconcile_budget_notional_used_day')"

{
  echo "Budget/Limits Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: Per-run notional cap"
  echo "  metric: ${DRILL_A_BLOCKED_LINE}"
  echo "  metric: ${DRILL_A_BUDGET_LINE}"
  echo '  block: $200+$200 recorded, third $200 blocked (total $600 > $500 per-run cap)'
  echo "  reason: max_notional_per_run -> RemediationBlockReason.MAX_NOTIONAL_PER_RUN"
  echo "  evaluated_on: attempted notional (used=400 + new=200 = 600 > cap=500)"
  echo "  state: drill_a_state.json persists calls_today=2, notional_today=400"
  echo ""
  echo "Drill B: Per-day notional cap (cross-run)"
  echo "  metric: ${DRILL_B_BLOCKED_LINE}"
  echo "  metric: ${DRILL_B_BUDGET_LINE}"
  echo '  block: Run1 $600 recorded, run2 $500 blocked (total $1100 > $1000 daily cap)'
  echo "  reason: max_notional_per_day -> RemediationBlockReason.MAX_NOTIONAL_PER_DAY"
  echo "  evaluated_on: attempted notional (today=600 + new=500 = 1100 > daily_cap=1000)"
  echo "  day_key: UTC date (datetime.now(UTC).strftime('%Y-%m-%d'))"
  echo "  run_reset: per-run counters cleared, per-day counters preserved"
  echo '  smaller_amount: $300 still allowed within remaining $400 daily budget'
  echo "  state: drill_b_state.json persists notional_today=600 across run boundary"
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
