#!/usr/bin/env bash
# fire_drill_sor.sh -- CI-safe SmartOrderRouter fire drill (Launch-14 PR3).
#
# Deterministically exercises the SOR decision paths with synthetic actions:
#   Drill A: CANCEL_REPLACE — happy path, BUY near market, action reaches port
#   Drill B: BLOCK — spread crossing, ROUTER_BLOCKED, 0 port calls
#   Drill C: NOOP — budget exhausted (router-only, direct route() call)
#   Drill D: Metrics contract smoke — verify SOR patterns in MetricsBuilder output
#
# Drills A/B use the REAL LiveEngineV0._process_action() — actual prod code path.
# Drill C uses route() directly (budgets not injectable through LiveEngineV0).
# Drill D verifies end-to-end metrics wiring against REQUIRED_METRICS_PATTERNS.
#
# No API keys needed. No network calls. No changes to src/grinder/.
# Takes ~2 seconds (pure CPU, no sleeps).
#
# Usage:
#   bash scripts/fire_drill_sor.sh
#
# Evidence artifacts saved under ${GRINDER_ARTIFACT_DIR:-.artifacts}/sor_fire_drill/<ts>/
# (gitignored via .artifacts/ rule). Do not commit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

export PYTHONPATH=src

# =========================================================================
# Evidence directory
# =========================================================================

ART_ROOT="${GRINDER_ARTIFACT_DIR:-.artifacts}"
EVIDENCE_TS="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="${ART_ROOT}/sor_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== SOR Fire Drill (Launch-14 PR3) ==="
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
# Drill A: CANCEL_REPLACE — happy path (BUY near market, port called)
# =========================================================================

echo "--- Drill A: CANCEL_REPLACE (happy path, BUY near market) ---"

DRILL_A_METRICS="$EVIDENCE_DIR/drill_a_metrics.txt"
DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"

PYTHONPATH=src python3 - "$DRILL_A_METRICS" <<'PY' 2>"$DRILL_A_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.live.engine import LiveEngineV0, LiveActionStatus, BlockReason
from grinder.live.config import LiveEngineConfig
from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.smart_order_router import ExchangeFilters
from grinder.execution.types import ActionType, ExecutionAction
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

# Reset SOR metrics for clean state
reset_sor_metrics()

# --- Config: armed, LIVE_TRADE, SOR enabled ---
config = LiveEngineConfig(
    armed=True,
    mode=SafeMode.LIVE_TRADE,
    kill_switch_active=False,
    symbol_whitelist=[],
    sor_enabled=True,
)

filters = ExchangeFilters(
    tick_size=Decimal("0.01"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("5"),
)

# --- Recording port (proves action reached exchange) ---
class RecordingPort:
    def __init__(self):
        self.calls = []

    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.calls.append(("place_order", {"symbol": symbol, "price": price}))
        return "SOR-DRILL-A-001"

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", {"order_id": order_id}))
        return True

    def replace_order(self, order_id, new_price, new_quantity, ts):
        self.calls.append(("replace_order", {"order_id": order_id}))
        return "SOR-DRILL-A-002"

port = RecordingPort()

# --- Paper engine returning a PLACE action ---
action = ExecutionAction(
    action_type=ActionType.PLACE,
    symbol="BTCUSDT",
    side=OrderSide.BUY,
    price=Decimal("50000.50"),  # Below ask (50001), near market, tick-aligned
    quantity=Decimal("0.01"),
    level_id=1,
    reason="GRID_ENTRY",
)

class FakePaperEngine:
    def process_snapshot(self, snapshot):
        class Out:
            actions = [action]
        return Out()

engine = LiveEngineV0(
    paper_engine=FakePaperEngine(),
    exchange_port=port,
    config=config,
    exchange_filters=filters,
)

# --- Run through REAL process_snapshot (sets _last_snapshot + calls _process_action) ---
snapshot = Snapshot(
    ts=1700000000,
    symbol="BTCUSDT",
    bid_price=Decimal("50000.00"),
    ask_price=Decimal("50001.00"),
    bid_qty=Decimal("1.0"),
    ask_qty=Decimal("1.0"),
    last_price=Decimal("50000.00"),
    last_qty=Decimal("0.5"),
)
output = engine.process_snapshot(snapshot)

result = output.live_actions[0]
print(
    f"drill_a_result: status={result.status.value} "
    f"block_reason={result.block_reason} "
    f"port_calls={len(port.calls)}",
    file=sys.stderr,
)

assert result.status == LiveActionStatus.EXECUTED, \
    f"Expected EXECUTED, got {result.status.value}"
assert len(port.calls) > 0, "Expected port to be called (place_order)"
assert port.calls[0][0] == "place_order", f"Expected place_order, got {port.calls[0][0]}"

# Verify SOR metric recorded
m = get_sor_metrics()
assert ("CANCEL_REPLACE", "NO_EXISTING_ORDER") in m.decisions, \
    f"Expected CANCEL_REPLACE decision, got {m.decisions}"
print(
    f"drill_a_metric: decision=CANCEL_REPLACE reason=NO_EXISTING_ORDER count={m.decisions[('CANCEL_REPLACE', 'NO_EXISTING_ORDER')]}",
    file=sys.stderr,
)

print("drill_a_PROVEN: CANCEL_REPLACE happy path, port.place_order called", file=sys.stderr)

# --- Metrics: render with safe risk state ---
reset_risk_metrics_state()
set_risk_metrics_state(RiskMetricsState(
    kill_switch_triggered=0,
    kill_switch_trips={},
    drawdown_pct=0.0,
))
builder = MetricsBuilder()
with open(metrics_path, "w") as f:
    f.write(builder.build())
reset_risk_metrics_state()
PY

# Shell assertions for Drill A
assert_contains "$DRILL_A_LOG" \
  "drill_a_result: status=EXECUTED" \
  "PLACE reaches port (status=EXECUTED)"

assert_contains "$DRILL_A_LOG" \
  "drill_a_PROVEN: CANCEL_REPLACE happy path" \
  "CANCEL_REPLACE proven (port.place_order called)"

assert_contains "$DRILL_A_LOG" \
  "drill_a_metric: decision=CANCEL_REPLACE reason=NO_EXISTING_ORDER" \
  "SOR metric recorded (CANCEL_REPLACE)"

assert_contains "$DRILL_A_METRICS" \
  'grinder_router_decision_total{decision="CANCEL_REPLACE",reason="NO_EXISTING_ORDER"}' \
  "metric: router_decision_total CANCEL_REPLACE in Prometheus output"

echo ""

# =========================================================================
# Drill B: BLOCK — spread crossing (ROUTER_BLOCKED, 0 port calls)
# =========================================================================

echo "--- Drill B: BLOCK (spread crossing, ROUTER_BLOCKED) ---"

DRILL_B_METRICS="$EVIDENCE_DIR/drill_b_metrics.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"

PYTHONPATH=src python3 - "$DRILL_B_METRICS" <<'PY' 2>"$DRILL_B_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.live.engine import LiveEngineV0, LiveActionStatus, BlockReason
from grinder.live.config import LiveEngineConfig
from grinder.connectors.live_connector import SafeMode
from grinder.contracts import Snapshot
from grinder.core import OrderSide
from grinder.execution.smart_order_router import ExchangeFilters
from grinder.execution.types import ActionType, ExecutionAction
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

# Reset SOR metrics
reset_sor_metrics()

config = LiveEngineConfig(
    armed=True,
    mode=SafeMode.LIVE_TRADE,
    kill_switch_active=False,
    symbol_whitelist=[],
    sor_enabled=True,
)

filters = ExchangeFilters(
    tick_size=Decimal("0.01"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("5"),
)

class RecordingPort:
    def __init__(self):
        self.calls = []

    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.calls.append("place_order")
        return "SOR-DRILL-B-001"

    def cancel_order(self, order_id):
        self.calls.append("cancel_order")
        return True

    def replace_order(self, order_id, new_price, new_quantity, ts):
        self.calls.append("replace_order")
        return "SOR-DRILL-B-002"

port = RecordingPort()

# BUY at 50001.00 >= best_ask (50001.00) -> WOULD_CROSS_SPREAD -> BLOCK
action = ExecutionAction(
    action_type=ActionType.PLACE,
    symbol="BTCUSDT",
    side=OrderSide.BUY,
    price=Decimal("50001.00"),
    quantity=Decimal("0.01"),
    level_id=1,
    reason="GRID_ENTRY",
)

class FakePaperEngine:
    def process_snapshot(self, snapshot):
        class Out:
            actions = [action]
        return Out()

engine = LiveEngineV0(
    paper_engine=FakePaperEngine(),
    exchange_port=port,
    config=config,
    exchange_filters=filters,
)

snapshot = Snapshot(
    ts=1700000000,
    symbol="BTCUSDT",
    bid_price=Decimal("50000.00"),
    ask_price=Decimal("50001.00"),
    bid_qty=Decimal("1.0"),
    ask_qty=Decimal("1.0"),
    last_price=Decimal("50000.00"),
    last_qty=Decimal("0.5"),
)
output = engine.process_snapshot(snapshot)

result = output.live_actions[0]
print(
    f"drill_b_result: status={result.status.value} "
    f"block_reason={result.block_reason.value if result.block_reason else None} "
    f"port_calls={len(port.calls)}",
    file=sys.stderr,
)

assert result.status == LiveActionStatus.BLOCKED, \
    f"Expected BLOCKED, got {result.status.value}"
assert result.block_reason == BlockReason.ROUTER_BLOCKED, \
    f"Expected ROUTER_BLOCKED, got {result.block_reason}"
assert len(port.calls) == 0, f"Expected 0 port calls, got {len(port.calls)}"

# Verify SOR metric
m = get_sor_metrics()
assert ("BLOCK", "WOULD_CROSS_SPREAD") in m.decisions, \
    f"Expected BLOCK decision, got {m.decisions}"
print(
    f"drill_b_metric: decision=BLOCK reason=WOULD_CROSS_SPREAD count={m.decisions[('BLOCK', 'WOULD_CROSS_SPREAD')]}",
    file=sys.stderr,
)

print("drill_b_PROVEN: BLOCK spread crossing, ROUTER_BLOCKED, 0 port calls", file=sys.stderr)

# --- Metrics ---
reset_risk_metrics_state()
set_risk_metrics_state(RiskMetricsState(
    kill_switch_triggered=0,
    kill_switch_trips={},
    drawdown_pct=0.0,
))
builder = MetricsBuilder()
with open(metrics_path, "w") as f:
    f.write(builder.build())
reset_risk_metrics_state()
PY

# Shell assertions for Drill B
assert_contains "$DRILL_B_LOG" \
  "drill_b_result: status=BLOCKED block_reason=ROUTER_BLOCKED port_calls=0" \
  "BLOCK: status=BLOCKED, block_reason=ROUTER_BLOCKED, 0 port calls"

assert_contains "$DRILL_B_LOG" \
  "drill_b_PROVEN: BLOCK spread crossing" \
  "BLOCK spread crossing proven"

assert_contains "$DRILL_B_LOG" \
  "drill_b_metric: decision=BLOCK reason=WOULD_CROSS_SPREAD" \
  "SOR metric recorded (BLOCK)"

assert_contains "$DRILL_B_METRICS" \
  'grinder_router_decision_total{decision="BLOCK",reason="WOULD_CROSS_SPREAD"}' \
  "metric: router_decision_total BLOCK in Prometheus output"

echo ""

# =========================================================================
# Drill C: NOOP — budget exhausted (router-only, direct route() call)
# =========================================================================

echo "--- Drill C: NOOP (budget exhausted, router-only) ---"

DRILL_C_METRICS="$EVIDENCE_DIR/drill_c_metrics.txt"
DRILL_C_LOG="$EVIDENCE_DIR/drill_c_log.txt"

PYTHONPATH=src python3 - "$DRILL_C_METRICS" <<'PY' 2>"$DRILL_C_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.execution.smart_order_router import (
    ExchangeFilters, MarketSnapshot, OrderIntent, RouterDecision,
    RouterInputs, UpdateBudgets, route,
)
from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

# Reset SOR metrics
reset_sor_metrics()

print("drill_c_mode: router-only (direct route() call)", file=sys.stderr)

inputs = RouterInputs(
    intent=OrderIntent(
        price=Decimal("50000.50"),
        qty=Decimal("0.01"),
        side="BUY",
    ),
    existing=None,
    market=MarketSnapshot(
        best_bid=Decimal("50000.00"),
        best_ask=Decimal("50001.00"),
    ),
    filters=ExchangeFilters(
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    ),
    budgets=UpdateBudgets(updates_remaining=0, cancel_replace_remaining=0),
)

result = route(inputs)
print(
    f"drill_c_result: decision={result.decision.value} reason={result.reason}",
    file=sys.stderr,
)

assert result.decision == RouterDecision.NOOP, \
    f"Expected NOOP, got {result.decision.value}"
assert result.reason == "RATE_LIMIT_THROTTLE", \
    f"Expected RATE_LIMIT_THROTTLE, got {result.reason}"

# Record metric manually (this is router-only, not through LiveEngineV0)
get_sor_metrics().record_decision("NOOP", "RATE_LIMIT_THROTTLE")
m = get_sor_metrics()
print(
    f"drill_c_metric: decision=NOOP reason=RATE_LIMIT_THROTTLE count={m.decisions[('NOOP', 'RATE_LIMIT_THROTTLE')]}",
    file=sys.stderr,
)

print("drill_c_PROVEN: NOOP budget exhausted (router-only)", file=sys.stderr)

# --- Metrics ---
reset_risk_metrics_state()
set_risk_metrics_state(RiskMetricsState(
    kill_switch_triggered=0,
    kill_switch_trips={},
    drawdown_pct=0.0,
))
builder = MetricsBuilder()
with open(metrics_path, "w") as f:
    f.write(builder.build())
reset_risk_metrics_state()
PY

# Shell assertions for Drill C
assert_contains "$DRILL_C_LOG" \
  "drill_c_mode: router-only (direct route() call)" \
  "Drill C is router-only (explicit marker)"

assert_contains "$DRILL_C_LOG" \
  "drill_c_result: decision=NOOP reason=RATE_LIMIT_THROTTLE" \
  "NOOP: decision=NOOP, reason=RATE_LIMIT_THROTTLE"

assert_contains "$DRILL_C_LOG" \
  "drill_c_PROVEN: NOOP budget exhausted" \
  "NOOP budget exhausted proven"

assert_contains "$DRILL_C_METRICS" \
  'grinder_router_decision_total{decision="NOOP",reason="RATE_LIMIT_THROTTLE"}' \
  "metric: router_decision_total NOOP in Prometheus output"

echo ""

# =========================================================================
# Drill D: Metrics contract smoke (P0-5)
# =========================================================================

echo "--- Drill D: Metrics contract smoke (SOR patterns in REQUIRED_METRICS_PATTERNS) ---"

DRILL_D_METRICS="$EVIDENCE_DIR/drill_d_metrics.txt"

PYTHONPATH=src python3 - "$DRILL_D_METRICS" <<'PY' 2>&1
import sys
from decimal import Decimal

from grinder.execution.sor_metrics import get_sor_metrics, reset_sor_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)
from grinder.observability.live_contract import REQUIRED_METRICS_PATTERNS

metrics_path = sys.argv[1]

# Reset and seed SOR metrics with a decision (so labeled series appear)
reset_sor_metrics()
get_sor_metrics().record_decision("CANCEL_REPLACE", "NO_EXISTING_ORDER")

# Build full metrics
reset_risk_metrics_state()
set_risk_metrics_state(RiskMetricsState(
    kill_switch_triggered=0,
    kill_switch_trips={},
    drawdown_pct=0.0,
))
builder = MetricsBuilder()
full_output = builder.build()
with open(metrics_path, "w") as f:
    f.write(full_output)
reset_risk_metrics_state()

# Check all SOR patterns
sor_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "router" in p.lower()]
print(f"drill_d_sor_pattern_count: {len(sor_patterns)}")

missing = []
for pattern in sor_patterns:
    if pattern not in full_output:
        missing.append(pattern)

if missing:
    print(f"drill_d_FAIL: {len(missing)} SOR patterns missing from MetricsBuilder output:")
    for p in missing:
        print(f"  MISSING: {p}")
    sys.exit(1)
else:
    print(f"drill_d_PROVEN: all {len(sor_patterns)} SOR patterns present in MetricsBuilder output")
PY

DRILL_D_RC=$?
if [[ "$DRILL_D_RC" -ne 0 ]]; then
  fail "Drill D: metrics contract smoke FAILED"
else
  pass "Drill D: all SOR patterns present in MetricsBuilder output"
fi

# Verify specific patterns in the metrics file
assert_contains "$DRILL_D_METRICS" \
  "# HELP grinder_router_decision_total" \
  "metric: HELP grinder_router_decision_total present"

assert_contains "$DRILL_D_METRICS" \
  "# TYPE grinder_router_decision_total" \
  "metric: TYPE grinder_router_decision_total present"

assert_contains "$DRILL_D_METRICS" \
  'grinder_router_decision_total{decision=' \
  "metric: router_decision_total series with decision label"

assert_contains "$DRILL_D_METRICS" \
  "# HELP grinder_router_amend_savings_total" \
  "metric: HELP grinder_router_amend_savings_total present"

assert_contains "$DRILL_D_METRICS" \
  "# TYPE grinder_router_amend_savings_total" \
  "metric: TYPE grinder_router_amend_savings_total present"

assert_contains "$DRILL_D_METRICS" \
  "grinder_router_amend_savings_total" \
  "metric: router_amend_savings_total present"

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

DRILL_A_CR_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_router_decision_total{decision="CANCEL_REPLACE"')"
DRILL_B_BLOCK_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_router_decision_total{decision="BLOCK"')"
DRILL_C_NOOP_LINE="$(get_metric_line "$DRILL_C_METRICS" 'grinder_router_decision_total{decision="NOOP"')"

{
  echo "SOR Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: CANCEL_REPLACE (happy path)"
  echo "  config: armed=True, mode=LIVE_TRADE, sor_enabled=True"
  echo "  action: PLACE BUY @ 50000.50 (bid=50000.00, ask=50001.00)"
  echo "  filters: tick=0.01, step=0.001, min_qty=0.001, min_notional=5"
  echo "  existing: None (no order state tracking in PR2/PR3)"
  echo "  decision: CANCEL_REPLACE (NO_EXISTING_ORDER)"
  echo "  result: status=EXECUTED, port.place_order called"
  echo "  metric: ${DRILL_A_CR_LINE}"
  echo "  code_path: LiveEngineV0.process_snapshot() -> _process_action() -> _apply_sor() -> _execute_action()"
  echo ""
  echo "Drill B: BLOCK (spread crossing)"
  echo "  action: PLACE BUY @ 50001.00 (>= best_ask 50001.00)"
  echo "  decision: BLOCK (WOULD_CROSS_SPREAD)"
  echo "  result: status=BLOCKED, block_reason=ROUTER_BLOCKED, 0 port calls"
  echo "  metric: ${DRILL_B_BLOCK_LINE}"
  echo "  code_path: LiveEngineV0.process_snapshot() -> _process_action() -> _apply_sor() -> BLOCKED"
  echo ""
  echo "Drill C: NOOP (budget exhausted) -- router-only (direct route() call)"
  echo "  budgets: updates_remaining=0, cancel_replace_remaining=0"
  echo "  decision: NOOP (RATE_LIMIT_THROTTLE)"
  echo "  metric: ${DRILL_C_NOOP_LINE}"
  echo "  note: budgets not injectable through LiveEngineV0, tested via direct route()"
  echo ""
  echo "Drill D: Metrics contract smoke"
  echo "  verified: all 6 SOR patterns from REQUIRED_METRICS_PATTERNS present in MetricsBuilder output"
  echo "  patterns: HELP/TYPE/series for grinder_router_decision_total, HELP/TYPE/value for grinder_router_amend_savings_total"
  echo ""
  echo "NOTE: Drills A/B use the REAL LiveEngineV0.process_snapshot() code path."
  echo "NOTE: Drill C is router-only (direct route() call) since budgets are not injectable through LiveEngineV0."
  echo "NOTE: Artifacts saved under ${EVIDENCE_DIR} (gitignored). Do not commit."
} > "$EVIDENCE_DIR/summary.txt"

# sha256sums (stable order, full hashes, excludes itself).
write_sha256sums

# =========================================================================
# Git guardrail
# =========================================================================

if git status --porcelain 2>/dev/null | grep -qF "${ART_ROOT}/"; then
  fail "${ART_ROOT}/ appears in git status --porcelain (gitignore broken)"
  git status --porcelain 2>/dev/null | grep -F "${ART_ROOT}/" | head -5
else
  pass "${ART_ROOT}/ not present in git status (gitignored)"
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
echo "NOTE: Artifacts saved under ${ART_ROOT}/... (gitignored). Do not commit."
echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
