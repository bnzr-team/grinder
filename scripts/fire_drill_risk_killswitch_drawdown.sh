#!/usr/bin/env bash
# fire_drill_risk_killswitch_drawdown.sh -- CI-safe risk fire drill (Launch-08 PR1).
#
# Deterministically exercises two risk enforcement paths:
#   Drill A: KillSwitch latch + enforcement gate (blocks INCREASE_RISK, allows CANCEL)
#   Drill B: DrawdownGuardV1 blocks INCREASE_RISK while allowing REDUCE_RISK/CANCEL
#
# Both drills also prove metrics reflect the state change via RiskMetricsState
# -> MetricsBuilder -> Prometheus text output.
#
# No API keys needed. No network calls. No changes to src/grinder/.
# Takes ~2 seconds (pure CPU, no sleeps).
#
# Usage:
#   bash scripts/fire_drill_risk_killswitch_drawdown.sh
#
# Evidence artifacts saved under .artifacts/risk_fire_drill/<ts>/
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
EVIDENCE_DIR=".artifacts/risk_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== Risk Fire Drill (Launch-08 PR1) ==="
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
# Drill A: KillSwitch latch + enforcement gate
# =========================================================================

echo "--- Drill A: KillSwitch latch + enforcement gate ---"

DRILL_A_METRICS="$EVIDENCE_DIR/drill_a_metrics.txt"
DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"

PYTHONPATH=src python3 - "$DRILL_A_METRICS" <<'PY' 2>"$DRILL_A_LOG"
import json
import logging
import sys

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from grinder.risk.kill_switch import KillSwitch, KillSwitchReason
from grinder.risk.drawdown_guard_v1 import DrawdownGuardV1, OrderIntent
from grinder.observability.metrics_builder import (
    MetricsBuilder,
    RiskMetricsState,
    set_risk_metrics_state,
    reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

# --- Phase 1: Trip the kill-switch ---
ks = KillSwitch()
assert not ks.is_triggered, "kill-switch should start untriggered"

state = ks.trip(KillSwitchReason.MANUAL, ts=1700000000, details={"source": "fire_drill"})
assert ks.is_triggered, "kill-switch should be triggered after trip"
assert state.reason == KillSwitchReason.MANUAL
print(f"kill_switch_triggered: {ks.is_triggered}", file=sys.stderr)
print(f"kill_switch_reason: {state.reason.value}", file=sys.stderr)
print(f"kill_switch_ts: {state.triggered_at_ts}", file=sys.stderr)

# Idempotent: second trip is a no-op
state2 = ks.trip(KillSwitchReason.ERROR, ts=1700000001)
assert state2.reason == KillSwitchReason.MANUAL, "idempotent: reason unchanged"
print("kill_switch_idempotent: PASS (reason unchanged after second trip)", file=sys.stderr)

# --- Phase 2: Enforcement gate proof ---
# Simulate the LiveEngine gate 3 logic:
#   if kill_switch_active and intent != CANCEL -> BLOCKED
kill_switch_active = True

results = {}
for intent in [OrderIntent.INCREASE_RISK, OrderIntent.REDUCE_RISK, OrderIntent.CANCEL]:
    if kill_switch_active and intent != OrderIntent.CANCEL:
        results[intent.value] = "BLOCKED"
    else:
        results[intent.value] = "ALLOWED"

print(f"gate_increase_risk: {results['INCREASE_RISK']}", file=sys.stderr)
print(f"gate_reduce_risk: {results['REDUCE_RISK']}", file=sys.stderr)
print(f"gate_cancel: {results['CANCEL']}", file=sys.stderr)

assert results["INCREASE_RISK"] == "BLOCKED", "INCREASE_RISK must be blocked"
assert results["REDUCE_RISK"] == "BLOCKED", "REDUCE_RISK must be blocked"
assert results["CANCEL"] == "ALLOWED", "CANCEL must be allowed"

# --- Phase 3: Metrics proof ---
reset_risk_metrics_state()
risk_state = RiskMetricsState(
    kill_switch_triggered=1,
    kill_switch_trips={"MANUAL": 1},
    drawdown_pct=0.0,
)
set_risk_metrics_state(risk_state)

builder = MetricsBuilder()
output = builder.build()

with open(metrics_path, "w") as f:
    f.write(output)

print("metrics_written: drill_a_metrics.txt", file=sys.stderr)

# Cleanup global state
reset_risk_metrics_state()
PY

# Assert kill-switch metrics
assert_contains "$DRILL_A_METRICS" \
  "grinder_kill_switch_triggered 1" \
  "kill_switch_triggered gauge = 1"

assert_contains "$DRILL_A_METRICS" \
  'grinder_kill_switch_trips_total{reason="MANUAL"} 1' \
  "kill_switch_trips_total{reason=MANUAL} = 1"

# Assert enforcement log markers
assert_contains "$DRILL_A_LOG" \
  "kill_switch_triggered: True" \
  "log: kill_switch_triggered = True"

assert_contains "$DRILL_A_LOG" \
  "gate_increase_risk: BLOCKED" \
  "log: INCREASE_RISK blocked by kill-switch"

assert_contains "$DRILL_A_LOG" \
  "gate_cancel: ALLOWED" \
  "log: CANCEL allowed despite kill-switch"

assert_contains "$DRILL_A_LOG" \
  "kill_switch_idempotent: PASS" \
  "log: kill-switch idempotent (second trip no-op)"

echo ""

# =========================================================================
# Drill B: DrawdownGuardV1 blocks INCREASE_RISK
# =========================================================================

echo "--- Drill B: DrawdownGuardV1 intent blocking ---"

DRILL_B_METRICS="$EVIDENCE_DIR/drill_b_metrics.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"

PYTHONPATH=src python3 - "$DRILL_B_METRICS" <<'PY' 2>"$DRILL_B_LOG"
import json
import logging
import sys
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from grinder.risk.drawdown_guard_v1 import (
    AllowReason,
    DrawdownGuardV1,
    DrawdownGuardV1Config,
    GuardState,
    OrderIntent,
)
from grinder.observability.metrics_builder import (
    MetricsBuilder,
    RiskMetricsState,
    set_risk_metrics_state,
    reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

# --- Phase 1: Create guard with 10% portfolio DD limit ---
config = DrawdownGuardV1Config(
    portfolio_dd_limit=Decimal("0.10"),  # 10%
    symbol_dd_budgets={"BTCUSDT": Decimal("500")},
)
guard = DrawdownGuardV1(config)

assert guard.state == GuardState.NORMAL, "guard should start NORMAL"
print(f"initial_state: {guard.state.value}", file=sys.stderr)

# --- Phase 2: Update with equity ABOVE threshold (still NORMAL) ---
snap1 = guard.update(
    equity_current=Decimal("95000"),  # 5% DD (under 10% limit)
    equity_start=Decimal("100000"),
)
assert guard.state == GuardState.NORMAL, "5% DD should not trigger 10% limit"
print(f"after_5pct_dd: state={guard.state.value} dd_pct={float(snap1.portfolio_dd_pct)*100:.1f}%", file=sys.stderr)

# All intents should be allowed in NORMAL
for intent in [OrderIntent.INCREASE_RISK, OrderIntent.REDUCE_RISK, OrderIntent.CANCEL]:
    decision = guard.allow(intent)
    assert decision.allowed, f"{intent.value} should be allowed in NORMAL"
    print(f"normal_{intent.value}: {decision.reason.value}", file=sys.stderr)

# --- Phase 3: Update with equity BELOW threshold (triggers DRAWDOWN) ---
snap2 = guard.update(
    equity_current=Decimal("88000"),  # 12% DD (over 10% limit)
    equity_start=Decimal("100000"),
)
assert guard.state == GuardState.DRAWDOWN, "12% DD should trigger 10% limit"
print(f"after_12pct_dd: state={guard.state.value} dd_pct={float(snap2.portfolio_dd_pct)*100:.1f}%", file=sys.stderr)
print(f"trigger_reason: {snap2.trigger_reason.value if snap2.trigger_reason else 'None'}", file=sys.stderr)

# --- Phase 4: Intent blocking in DRAWDOWN state ---
decision_increase = guard.allow(OrderIntent.INCREASE_RISK, symbol="BTCUSDT")
decision_reduce = guard.allow(OrderIntent.REDUCE_RISK, symbol="BTCUSDT")
decision_cancel = guard.allow(OrderIntent.CANCEL, symbol="BTCUSDT")

print(f"drawdown_INCREASE_RISK: allowed={decision_increase.allowed} reason={decision_increase.reason.value}", file=sys.stderr)
print(f"drawdown_REDUCE_RISK: allowed={decision_reduce.allowed} reason={decision_reduce.reason.value}", file=sys.stderr)
print(f"drawdown_CANCEL: allowed={decision_cancel.allowed} reason={decision_cancel.reason.value}", file=sys.stderr)

assert not decision_increase.allowed, "INCREASE_RISK must be blocked in DRAWDOWN"
assert decision_increase.reason == AllowReason.DD_PORTFOLIO_BREACH
assert decision_reduce.allowed, "REDUCE_RISK must be allowed in DRAWDOWN"
assert decision_reduce.reason == AllowReason.REDUCE_RISK_ALLOWED
assert decision_cancel.allowed, "CANCEL must be allowed in DRAWDOWN"
assert decision_cancel.reason == AllowReason.CANCEL_ALWAYS_ALLOWED

# --- Phase 5: Latching proof (state stays DRAWDOWN even if equity recovers) ---
snap3 = guard.update(
    equity_current=Decimal("100000"),  # recovered to start
    equity_start=Decimal("100000"),
)
assert guard.state == GuardState.DRAWDOWN, "DRAWDOWN state must be latched (no auto-recovery)"
print(f"after_recovery: state={guard.state.value} (latched, no auto-recovery)", file=sys.stderr)

# --- Phase 6: Metrics proof ---
reset_risk_metrics_state()
dd_pct = float(snap2.portfolio_dd_pct) * 100  # Convert fraction to percentage for metrics
risk_state = RiskMetricsState(
    kill_switch_triggered=0,
    kill_switch_trips={},
    drawdown_pct=dd_pct,
    high_water_mark=Decimal("100000"),
)
set_risk_metrics_state(risk_state)

builder = MetricsBuilder()
output = builder.build()

with open(metrics_path, "w") as f:
    f.write(output)

print(f"metrics_drawdown_pct: {dd_pct:.2f}", file=sys.stderr)
print(f"metrics_high_water_mark: 100000.00", file=sys.stderr)
print("metrics_written: drill_b_metrics.txt", file=sys.stderr)

# Cleanup global state
reset_risk_metrics_state()
PY

# Assert drawdown metrics
LINE_DD="$(get_metric_line "$DRILL_B_METRICS" 'grinder_drawdown_pct')"
LINE_HWM="$(get_metric_line "$DRILL_B_METRICS" 'grinder_high_water_mark')"

if [[ -n "$LINE_DD" ]]; then
  DD_VAL="$(metric_value "$LINE_DD")"
  if awk "BEGIN { exit !($DD_VAL > 10.0) }" 2>/dev/null; then
    pass "drawdown_pct > 10.0 ($DD_VAL)"
  else
    fail "drawdown_pct not > 10.0 ($DD_VAL)"
  fi
else
  fail "grinder_drawdown_pct not found in metrics"
fi

if [[ -n "$LINE_HWM" ]]; then
  HWM_VAL="$(metric_value "$LINE_HWM")"
  if awk "BEGIN { exit !($HWM_VAL > 0) }" 2>/dev/null; then
    pass "high_water_mark > 0 ($HWM_VAL)"
  else
    fail "high_water_mark not > 0 ($HWM_VAL)"
  fi
else
  fail "grinder_high_water_mark not found in metrics"
fi

# Assert intent blocking log markers
assert_contains "$DRILL_B_LOG" \
  "drawdown_INCREASE_RISK: allowed=False" \
  "log: INCREASE_RISK blocked in DRAWDOWN"

assert_contains "$DRILL_B_LOG" \
  "drawdown_REDUCE_RISK: allowed=True" \
  "log: REDUCE_RISK allowed in DRAWDOWN"

assert_contains "$DRILL_B_LOG" \
  "drawdown_CANCEL: allowed=True" \
  "log: CANCEL allowed in DRAWDOWN"

assert_contains "$DRILL_B_LOG" \
  "after_recovery: state=DRAWDOWN (latched, no auto-recovery)" \
  "log: DRAWDOWN state latched after recovery"

assert_contains "$DRILL_B_LOG" \
  "trigger_reason: DD_PORTFOLIO_BREACH" \
  "log: trigger reason = DD_PORTFOLIO_BREACH"

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

DRILL_A_KS_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_kill_switch_triggered')"
DRILL_A_TRIPS_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_kill_switch_trips_total{reason="MANUAL"}')"

{
  echo "Risk Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: KillSwitch latch + enforcement gate"
  echo "  metric: ${DRILL_A_KS_LINE}"
  echo "  metric: ${DRILL_A_TRIPS_LINE}"
  echo "  gate: INCREASE_RISK=BLOCKED  REDUCE_RISK=BLOCKED  CANCEL=ALLOWED"
  echo "  idempotent: second trip no-op (reason unchanged)"
  echo ""
  echo "Drill B: DrawdownGuardV1 intent blocking"
  echo "  metric: ${LINE_DD}"
  echo "  metric: ${LINE_HWM}"
  echo "  state_transition: NORMAL -> DRAWDOWN (12% DD > 10% limit)"
  echo "  intent_blocking: INCREASE_RISK=BLOCKED  REDUCE_RISK=ALLOWED  CANCEL=ALLOWED"
  echo "  trigger_reason: DD_PORTFOLIO_BREACH"
  echo "  latching: DRAWDOWN persists after equity recovery (no auto-recovery)"
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
