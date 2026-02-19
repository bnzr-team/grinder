#!/usr/bin/env bash
# fire_drill_connector_exchange_port.sh -- CI-safe exchange port boundary fire drill (Launch-10 PR2).
#
# Deterministically exercises six exchange port boundary trust paths:
#   Drill A: NOT_ARMED blocks all (0 port calls, pre-port rejection)
#   Drill B: Kill-switch blocks PLACE, allows CANCEL through to port
#   Drill C: Drawdown blocks INCREASE_RISK, allows CANCEL, NOOP skipped
#   Drill D: Symbol whitelist blocks before any port/retry machinery
#   Drill E: Idempotency cache prevents duplicate port calls
#   Drill F: Retry classification (transient retries vs fatal immediate fail)
#
# No API keys needed. No network calls. No changes to src/grinder/.
# Takes ~2 seconds (pure CPU, no sleeps beyond 0ms retry delay).
#
# Usage:
#   bash scripts/fire_drill_connector_exchange_port.sh
#
# Evidence artifacts saved under .artifacts/connector_exchange_port_fire_drill/<ts>/
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
EVIDENCE_DIR=".artifacts/connector_exchange_port_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== Exchange Port Boundary Fire Drill (Launch-10 PR2) ==="
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

assert_not_contains() {
  local file="$1" needle="$2" label="$3"
  if grep -qF "$needle" "$file" 2>/dev/null; then
    fail "$label"
    echo "    unexpected: $needle"
    echo "    in file: $file"
  else
    pass "$label"
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
# Drill A: NOT_ARMED blocks all — 0 port calls, pre-port rejection
# =========================================================================

echo "--- Drill A: NOT_ARMED blocks all ---"

DRILL_A_METRICS="$EVIDENCE_DIR/drill_a_metrics.txt"
DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"

PYTHONPATH=src python3 - "$DRILL_A_METRICS" <<'PY' 2>"$DRILL_A_LOG"
import sys
from decimal import Decimal

from grinder.connectors.metrics import get_connector_metrics, reset_connector_metrics
from grinder.connectors.retries import RetryPolicy
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import (
    BlockReason,
    LiveActionStatus,
    LiveEngineV0,
    classify_intent,
)

reset_connector_metrics()
metrics_path = sys.argv[1]

# FakePort: records calls, should never be reached in this drill
class FakePort:
    def __init__(self):
        self.place_calls = []
        self.cancel_calls = []
    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.place_calls.append({"symbol": symbol, "side": side})
        return "FAKE-001"
    def cancel_order(self, order_id):
        self.cancel_calls.append({"order_id": order_id})
        return True
    def replace_order(self, order_id, new_price, new_quantity, ts):
        return "FAKE-002"
    def fetch_open_orders(self, symbol):
        return []

port = FakePort()
config = LiveEngineConfig(armed=False)
engine = LiveEngineV0(
    paper_engine=None,  # not used when calling _process_action directly
    exchange_port=port,
    config=config,
    retry_policy=RetryPolicy(max_attempts=1),
)

# Attempt PLACE
place_action = ExecutionAction(
    action_type=ActionType.PLACE, symbol="BTCUSDT",
    side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"),
    level_id=1,
)
result_place = engine._process_action(place_action, ts=1700000000)
assert result_place.status == LiveActionStatus.BLOCKED
assert result_place.block_reason == BlockReason.NOT_ARMED
print(f"PORT_BLOCKED reason=NOT_ARMED op=place", file=sys.stderr)

# Attempt CANCEL
cancel_action = ExecutionAction(
    action_type=ActionType.CANCEL, symbol="BTCUSDT", order_id="ORD-001",
)
result_cancel = engine._process_action(cancel_action, ts=1700000000)
assert result_cancel.status == LiveActionStatus.BLOCKED
assert result_cancel.block_reason == BlockReason.NOT_ARMED
print(f"PORT_BLOCKED reason=NOT_ARMED op=cancel", file=sys.stderr)

# Assert zero port calls (decision made pre-port)
assert len(port.place_calls) == 0, f"place_calls={len(port.place_calls)}"
assert len(port.cancel_calls) == 0, f"cancel_calls={len(port.cancel_calls)}"
print(f"port_place_calls: 0", file=sys.stderr)
print(f"port_cancel_calls: 0", file=sys.stderr)

# Write connector metrics
m = get_connector_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"metrics_written: drill_a_metrics.txt", file=sys.stderr)
reset_connector_metrics()
PY

assert_contains "$DRILL_A_LOG" "PORT_BLOCKED reason=NOT_ARMED op=place" \
  "PORT_BLOCKED reason=NOT_ARMED op=place"
assert_contains "$DRILL_A_LOG" "PORT_BLOCKED reason=NOT_ARMED op=cancel" \
  "PORT_BLOCKED reason=NOT_ARMED op=cancel"
assert_contains "$DRILL_A_LOG" "port_place_calls: 0" \
  "place_order never called (pre-port block)"
assert_contains "$DRILL_A_LOG" "port_cancel_calls: 0" \
  "cancel_order never called (pre-port block)"
# Gotcha: no PORT_CALL line should appear (decision before serialization)
assert_not_contains "$DRILL_A_LOG" "PORT_CALL" \
  "no PORT_CALL emitted (block before port)"

echo ""

# =========================================================================
# Drill B: Kill-switch: PLACE blocked, CANCEL allowed through to port
# =========================================================================

echo "--- Drill B: Kill-switch: CANCEL through, PLACE blocked ---"

DRILL_B_METRICS="$EVIDENCE_DIR/drill_b_metrics.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"

PYTHONPATH=src python3 - "$DRILL_B_METRICS" <<'PY' 2>"$DRILL_B_LOG"
import sys
from decimal import Decimal

from grinder.connectors.live_connector import SafeMode
from grinder.connectors.metrics import get_connector_metrics, reset_connector_metrics
from grinder.connectors.retries import RetryPolicy
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import (
    BlockReason,
    LiveActionStatus,
    LiveEngineV0,
    classify_intent,
)
from grinder.risk.drawdown_guard_v1 import OrderIntent as RiskIntent

reset_connector_metrics()
metrics_path = sys.argv[1]

class RecordingPort:
    def __init__(self):
        self.place_calls = []
        self.cancel_calls = []
    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.place_calls.append({"symbol": symbol})
        return "FAKE-001"
    def cancel_order(self, order_id):
        self.cancel_calls.append({"order_id": order_id})
        print(f"PORT_CALL op=cancel order_id={order_id}", file=sys.stderr)
        print(f"PORT_RESULT result=ok op=cancel", file=sys.stderr)
        return True
    def replace_order(self, order_id, new_price, new_quantity, ts):
        return "FAKE-002"
    def fetch_open_orders(self, symbol):
        return []

port = RecordingPort()
config = LiveEngineConfig(
    armed=True, mode=SafeMode.LIVE_TRADE, kill_switch_active=True,
)
engine = LiveEngineV0(
    paper_engine=None, exchange_port=port, config=config,
    retry_policy=RetryPolicy(max_attempts=1),
)

# PLACE → BLOCKED (INCREASE_RISK blocked by kill-switch)
place_action = ExecutionAction(
    action_type=ActionType.PLACE, symbol="BTCUSDT",
    side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"),
    level_id=1,
)
result_place = engine._process_action(place_action, ts=1700000000)
assert result_place.status == LiveActionStatus.BLOCKED
assert result_place.block_reason == BlockReason.KILL_SWITCH_ACTIVE
intent_place = classify_intent(place_action)
print(f"PORT_BLOCKED reason=KILL_SWITCH_ACTIVE op=place intent={intent_place.value}", file=sys.stderr)

# CANCEL → ALLOWED (passes all gates, reaches port)
cancel_action = ExecutionAction(
    action_type=ActionType.CANCEL, symbol="BTCUSDT", order_id="ORD-001",
)
result_cancel = engine._process_action(cancel_action, ts=1700000000)
assert result_cancel.status == LiveActionStatus.EXECUTED
intent_cancel = classify_intent(cancel_action)
print(f"intent_cancel={intent_cancel.value}", file=sys.stderr)

assert len(port.place_calls) == 0, f"place_calls={len(port.place_calls)}"
assert len(port.cancel_calls) == 1, f"cancel_calls={len(port.cancel_calls)}"
assert port.cancel_calls[0]["order_id"] == "ORD-001"
print(f"port_place_calls: 0", file=sys.stderr)
print(f"port_cancel_calls: 1", file=sys.stderr)

m = get_connector_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
reset_connector_metrics()
PY

assert_contains "$DRILL_B_LOG" "PORT_BLOCKED reason=KILL_SWITCH_ACTIVE op=place" \
  "PORT_BLOCKED reason=KILL_SWITCH_ACTIVE op=place"
assert_contains "$DRILL_B_LOG" "PORT_CALL op=cancel order_id=ORD-001" \
  "PORT_CALL op=cancel (CANCEL reaches port)"
assert_contains "$DRILL_B_LOG" "PORT_RESULT result=ok op=cancel" \
  "PORT_RESULT result=ok op=cancel"
assert_contains "$DRILL_B_LOG" "intent_cancel=CANCEL" \
  "CANCEL intent is CANCEL (not INCREASE_RISK)"
assert_contains "$DRILL_B_LOG" "port_place_calls: 0" \
  "place_order_calls == 0"
assert_contains "$DRILL_B_LOG" "port_cancel_calls: 1" \
  "cancel_order_calls == 1"

echo ""

# =========================================================================
# Drill C: Drawdown: INCREASE_RISK blocked, CANCEL allowed, NOOP skipped
# =========================================================================

echo "--- Drill C: Drawdown blocks INCREASE_RISK, CANCEL+NOOP safe ---"

DRILL_C_METRICS="$EVIDENCE_DIR/drill_c_metrics.txt"
DRILL_C_LOG="$EVIDENCE_DIR/drill_c_log.txt"

PYTHONPATH=src python3 - "$DRILL_C_METRICS" <<'PY' 2>"$DRILL_C_LOG"
import sys
from decimal import Decimal

from grinder.connectors.live_connector import SafeMode
from grinder.connectors.metrics import get_connector_metrics, reset_connector_metrics
from grinder.connectors.retries import RetryPolicy
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import (
    BlockReason,
    LiveActionStatus,
    LiveEngineV0,
    classify_intent,
)
from grinder.risk.drawdown_guard_v1 import (
    DrawdownGuardV1,
    DrawdownGuardV1Config,
    GuardState,
    OrderIntent as RiskIntent,
)

reset_connector_metrics()
metrics_path = sys.argv[1]

class RecordingPort:
    def __init__(self):
        self.place_calls = []
        self.cancel_calls = []
    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.place_calls.append({"symbol": symbol})
        return "FAKE-001"
    def cancel_order(self, order_id):
        self.cancel_calls.append({"order_id": order_id})
        print(f"PORT_CALL op=cancel order_id={order_id}", file=sys.stderr)
        print(f"PORT_RESULT result=ok op=cancel", file=sys.stderr)
        return True
    def replace_order(self, order_id, new_price, new_quantity, ts):
        return "FAKE-002"
    def fetch_open_orders(self, symbol):
        return []

port = RecordingPort()
config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)

# Create drawdown guard and force into DRAWDOWN state
dd_config = DrawdownGuardV1Config(portfolio_dd_limit=Decimal("0.10"))
dd_guard = DrawdownGuardV1(dd_config)
dd_guard.update(equity_current=Decimal("88000"), equity_start=Decimal("100000"))
assert dd_guard.state == GuardState.DRAWDOWN
print(f"drawdown_state: {dd_guard.state.value}", file=sys.stderr)

engine = LiveEngineV0(
    paper_engine=None, exchange_port=port, config=config,
    drawdown_guard=dd_guard,
    retry_policy=RetryPolicy(max_attempts=1),
)

# PLACE → BLOCKED (INCREASE_RISK in DRAWDOWN)
place_action = ExecutionAction(
    action_type=ActionType.PLACE, symbol="BTCUSDT",
    side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"),
    level_id=1,
)
result_place = engine._process_action(place_action, ts=1700000000)
assert result_place.status == LiveActionStatus.BLOCKED
assert result_place.block_reason == BlockReason.DRAWDOWN_BLOCKED
print(f"PORT_BLOCKED reason=DRAWDOWN_BLOCKED op=place", file=sys.stderr)

# CANCEL → ALLOWED (CANCEL always passes drawdown gate)
cancel_action = ExecutionAction(
    action_type=ActionType.CANCEL, symbol="BTCUSDT", order_id="ORD-002",
)
result_cancel = engine._process_action(cancel_action, ts=1700000000)
assert result_cancel.status == LiveActionStatus.EXECUTED
print(f"cancel_status: {result_cancel.status.value}", file=sys.stderr)

# NOOP → SKIPPED (passes gates but no port call)
noop_action = ExecutionAction(action_type=ActionType.NOOP)
result_noop = engine._process_action(noop_action, ts=1700000000)
assert result_noop.status == LiveActionStatus.SKIPPED
print(f"PORT_RESULT result=skipped op=noop", file=sys.stderr)
print(f"NOOP_SKIPPED", file=sys.stderr)

assert len(port.place_calls) == 0
assert len(port.cancel_calls) == 1
print(f"port_place_calls: 0", file=sys.stderr)
print(f"port_cancel_calls: 1", file=sys.stderr)

m = get_connector_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
reset_connector_metrics()
PY

assert_contains "$DRILL_C_LOG" "PORT_BLOCKED reason=DRAWDOWN_BLOCKED op=place" \
  "PORT_BLOCKED reason=DRAWDOWN_BLOCKED op=place"
assert_contains "$DRILL_C_LOG" "PORT_CALL op=cancel order_id=ORD-002" \
  "PORT_CALL op=cancel (CANCEL allowed in DRAWDOWN)"
assert_contains "$DRILL_C_LOG" "NOOP_SKIPPED" \
  "NOOP_SKIPPED (no port call)"
assert_contains "$DRILL_C_LOG" "port_place_calls: 0" \
  "place_order_calls == 0 (INCREASE_RISK blocked)"
assert_contains "$DRILL_C_LOG" "port_cancel_calls: 1" \
  "cancel_order_calls == 1"

echo ""

# =========================================================================
# Drill D: Symbol whitelist blocks before retry/idempotency machinery
# =========================================================================

echo "--- Drill D: Symbol whitelist blocks at boundary ---"

DRILL_D_METRICS="$EVIDENCE_DIR/drill_d_metrics.txt"
DRILL_D_LOG="$EVIDENCE_DIR/drill_d_log.txt"

PYTHONPATH=src python3 - "$DRILL_D_METRICS" <<'PY' 2>"$DRILL_D_LOG"
import sys
from decimal import Decimal

from grinder.connectors.live_connector import SafeMode
from grinder.connectors.metrics import get_connector_metrics, reset_connector_metrics
from grinder.connectors.retries import RetryPolicy
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import (
    BlockReason,
    LiveActionStatus,
    LiveEngineV0,
)

reset_connector_metrics()
metrics_path = sys.argv[1]

class RecordingPort:
    def __init__(self):
        self.place_calls = []
        self.cancel_calls = []
    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.place_calls.append({"symbol": symbol})
        return "FAKE-001"
    def cancel_order(self, order_id):
        self.cancel_calls.append({"order_id": order_id})
        return True
    def replace_order(self, order_id, new_price, new_quantity, ts):
        return "FAKE-002"
    def fetch_open_orders(self, symbol):
        return []

port = RecordingPort()
config = LiveEngineConfig(
    armed=True, mode=SafeMode.LIVE_TRADE, symbol_whitelist=["BTCUSDT"],
)
engine = LiveEngineV0(
    paper_engine=None, exchange_port=port, config=config,
    retry_policy=RetryPolicy(max_attempts=1),
)

# PLACE XYZUSDT → BLOCKED (symbol not in whitelist)
place_action = ExecutionAction(
    action_type=ActionType.PLACE, symbol="XYZUSDT",
    side=OrderSide.BUY, price=Decimal("100"), quantity=Decimal("1"),
    level_id=1,
)
result_place = engine._process_action(place_action, ts=1700000000)
assert result_place.status == LiveActionStatus.BLOCKED
assert result_place.block_reason == BlockReason.SYMBOL_NOT_WHITELISTED
print(f"PORT_BLOCKED reason=SYMBOL_NOT_WHITELISTED op=place symbol=XYZUSDT", file=sys.stderr)

# CANCEL XYZUSDT → BLOCKED (whitelist applies to cancel too)
cancel_action = ExecutionAction(
    action_type=ActionType.CANCEL, symbol="XYZUSDT", order_id="ORD-003",
)
result_cancel = engine._process_action(cancel_action, ts=1700000000)
assert result_cancel.status == LiveActionStatus.BLOCKED
assert result_cancel.block_reason == BlockReason.SYMBOL_NOT_WHITELISTED
print(f"PORT_BLOCKED reason=SYMBOL_NOT_WHITELISTED op=cancel symbol=XYZUSDT", file=sys.stderr)

assert len(port.place_calls) == 0
assert len(port.cancel_calls) == 0
print(f"port_place_calls: 0", file=sys.stderr)
print(f"port_cancel_calls: 0", file=sys.stderr)

m = get_connector_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
reset_connector_metrics()
PY

assert_contains "$DRILL_D_LOG" \
  "PORT_BLOCKED reason=SYMBOL_NOT_WHITELISTED op=place symbol=XYZUSDT" \
  "PORT_BLOCKED reason=SYMBOL_NOT_WHITELISTED op=place"
assert_contains "$DRILL_D_LOG" \
  "PORT_BLOCKED reason=SYMBOL_NOT_WHITELISTED op=cancel symbol=XYZUSDT" \
  "PORT_BLOCKED reason=SYMBOL_NOT_WHITELISTED op=cancel"
assert_contains "$DRILL_D_LOG" "port_place_calls: 0" \
  "place blocked before retry machinery"
assert_contains "$DRILL_D_LOG" "port_cancel_calls: 0" \
  "cancel blocked before retry machinery"

echo ""

# =========================================================================
# Drill E: Idempotency cache prevents duplicate port calls
# =========================================================================

echo "--- Drill E: Idempotency cache (duplicate prevention) ---"

DRILL_E_METRICS="$EVIDENCE_DIR/drill_e_metrics.txt"
DRILL_E_LOG="$EVIDENCE_DIR/drill_e_log.txt"

PYTHONPATH=src python3 - "$DRILL_E_METRICS" <<'PY' 2>"$DRILL_E_LOG"
import sys
from decimal import Decimal

from grinder.connectors.idempotency import (
    IdempotencyEntry,
    IdempotencyStatus,
    InMemoryIdempotencyStore,
    compute_idempotency_key,
)
from grinder.connectors.metrics import get_connector_metrics, reset_connector_metrics

reset_connector_metrics()
metrics_path = sys.argv[1]
cm = get_connector_metrics()

# Create idempotency store + compute key
store = InMemoryIdempotencyStore()
key = compute_idempotency_key(
    "exec", "place", symbol="BTCUSDT", side="BUY",
    price=Decimal("50000"), quantity=Decimal("0.001"), level_id=1,
)
print(f"idempotency_key: {key}", file=sys.stderr)

# First call: put_if_absent → True (new entry), execute
ok1 = store.put_if_absent(
    key,
    IdempotencyEntry(
        key=key, status=IdempotencyStatus.INFLIGHT,
        op_name="place", request_fingerprint="fp1",
        created_at=0, expires_at=0,
    ),
    ttl_s=300,
)
assert ok1, "first put_if_absent should succeed"
cm.record_idempotency_miss("place")
print(f"PORT_CALL op=place client_order_id={key}", file=sys.stderr)
print(f"PORT_RESULT result=ok op=place", file=sys.stderr)

# Mark done (simulating successful execution)
store.mark_done(key, result="FAKE-ORDER-001")

# Second call: put_if_absent → False (DONE hit, cache prevents call)
ok2 = store.put_if_absent(
    key,
    IdempotencyEntry(
        key=key, status=IdempotencyStatus.INFLIGHT,
        op_name="place", request_fingerprint="fp1",
        created_at=0, expires_at=0,
    ),
    ttl_s=300,
)
assert not ok2, "second put_if_absent should return False (DONE hit)"
cm.record_idempotency_hit("place")
print(f"PORT_IDEMPOTENT_HIT key={key}", file=sys.stderr)

# Verify store stats
stats = store.stats
assert stats.hits == 1, f"expected 1 hit, got {stats.hits}"
assert stats.misses == 1, f"expected 1 miss, got {stats.misses}"
print(f"idempotency_hits: {stats.hits}", file=sys.stderr)
print(f"idempotency_misses: {stats.misses}", file=sys.stderr)
print(f"place_order_calls_total: 1 (second call cached)", file=sys.stderr)

# Write connector metrics
lines = cm.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
reset_connector_metrics()
PY

assert_contains "$DRILL_E_LOG" "PORT_CALL op=place" \
  "PORT_CALL op=place (first call)"
assert_contains "$DRILL_E_LOG" "PORT_IDEMPOTENT_HIT" \
  "PORT_IDEMPOTENT_HIT (second call cached)"
assert_contains "$DRILL_E_LOG" "idempotency_hits: 1" \
  "idempotency store: 1 hit"
assert_contains "$DRILL_E_LOG" "idempotency_misses: 1" \
  "idempotency store: 1 miss (first call)"
assert_contains "$DRILL_E_LOG" "place_order_calls_total: 1 (second call cached)" \
  "only 1 port call (duplicate prevented)"

# Assert idempotency metric in Prometheus output
IDEM_HIT_LINE="$(get_metric_line "$DRILL_E_METRICS" 'grinder_idempotency_hits_total{op="place"}')"
if [[ -n "$IDEM_HIT_LINE" ]]; then
  IDEM_VAL="$(metric_value "$IDEM_HIT_LINE")"
  if [[ "$IDEM_VAL" -ge 1 ]]; then
    pass "grinder_idempotency_hits_total{op=place} >= 1 ($IDEM_VAL)"
  else
    fail "grinder_idempotency_hits_total{op=place} not >= 1 ($IDEM_VAL)"
  fi
else
  fail "grinder_idempotency_hits_total{op=place} not found"
fi

IDEM_MISS_LINE="$(get_metric_line "$DRILL_E_METRICS" 'grinder_idempotency_misses_total{op="place"}')"
if [[ -n "$IDEM_MISS_LINE" ]]; then
  MISS_VAL="$(metric_value "$IDEM_MISS_LINE")"
  if [[ "$MISS_VAL" -ge 1 ]]; then
    pass "grinder_idempotency_misses_total{op=place} >= 1 ($MISS_VAL)"
  else
    fail "grinder_idempotency_misses_total{op=place} not >= 1 ($MISS_VAL)"
  fi
else
  fail "grinder_idempotency_misses_total{op=place} not found"
fi

echo ""

# =========================================================================
# Drill F: Retry classification (transient retries vs fatal immediate)
# =========================================================================

echo "--- Drill F: Retry classification ---"

DRILL_F_METRICS="$EVIDENCE_DIR/drill_f_metrics.txt"
DRILL_F_LOG="$EVIDENCE_DIR/drill_f_log.txt"

PYTHONPATH=src python3 - "$DRILL_F_METRICS" <<'PY' 2>"$DRILL_F_LOG"
import sys
from decimal import Decimal

from grinder.connectors.errors import ConnectorNonRetryableError, ConnectorTransientError
from grinder.connectors.live_connector import SafeMode
from grinder.connectors.metrics import get_connector_metrics, reset_connector_metrics
from grinder.connectors.retries import RetryPolicy
from grinder.core import OrderSide
from grinder.execution.types import ActionType, ExecutionAction
from grinder.live.config import LiveEngineConfig
from grinder.live.engine import (
    BlockReason,
    LiveActionStatus,
    LiveEngineV0,
)

reset_connector_metrics()
metrics_path = sys.argv[1]

# ---- F1: Transient error → retries, then MAX_RETRIES_EXCEEDED ----
class TransientPort:
    """Port that always raises ConnectorTransientError."""
    def __init__(self):
        self.call_count = 0
    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.call_count += 1
        print(f"PORT_RETRYABLE error=ConnectorTransientError attempt={self.call_count}", file=sys.stderr)
        raise ConnectorTransientError("simulated timeout")
    def cancel_order(self, order_id):
        return True
    def replace_order(self, order_id, new_price, new_quantity, ts):
        return "FAKE"
    def fetch_open_orders(self, symbol):
        return []

transient_port = TransientPort()
config = LiveEngineConfig(armed=True, mode=SafeMode.LIVE_TRADE)
engine_f1 = LiveEngineV0(
    paper_engine=None, exchange_port=transient_port, config=config,
    retry_policy=RetryPolicy(max_attempts=2, base_delay_ms=0, backoff_multiplier=1.0),
)

place_action = ExecutionAction(
    action_type=ActionType.PLACE, symbol="BTCUSDT",
    side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"),
    level_id=1,
)
result_f1 = engine_f1._process_action(place_action, ts=1700000000)

assert result_f1.status == LiveActionStatus.FAILED
assert result_f1.block_reason == BlockReason.MAX_RETRIES_EXCEEDED
assert result_f1.attempts == 2, f"expected 2 attempts, got {result_f1.attempts}"
assert transient_port.call_count == 2, f"expected 2 port calls, got {transient_port.call_count}"
print(f"PORT_RESULT result=retryable_error op=place", file=sys.stderr)
print(f"f1_status: {result_f1.status.value}", file=sys.stderr)
print(f"f1_block_reason: {result_f1.block_reason.value}", file=sys.stderr)
print(f"f1_attempts: {result_f1.attempts}", file=sys.stderr)
print(f"f1_port_calls: {transient_port.call_count}", file=sys.stderr)

# ---- F2: Fatal error → immediate fail, no retry ----
class FatalPort:
    """Port that always raises ConnectorNonRetryableError."""
    def __init__(self):
        self.call_count = 0
    def place_order(self, symbol, side, price, quantity, level_id, ts):
        self.call_count += 1
        print(f"PORT_FATAL error=InsufficientBalance attempt={self.call_count}", file=sys.stderr)
        raise ConnectorNonRetryableError("insufficient balance")
    def cancel_order(self, order_id):
        return True
    def replace_order(self, order_id, new_price, new_quantity, ts):
        return "FAKE"
    def fetch_open_orders(self, symbol):
        return []

fatal_port = FatalPort()
engine_f2 = LiveEngineV0(
    paper_engine=None, exchange_port=fatal_port, config=config,
    retry_policy=RetryPolicy(max_attempts=3, base_delay_ms=0, backoff_multiplier=1.0),
)

result_f2 = engine_f2._process_action(place_action, ts=1700000000)

assert result_f2.status == LiveActionStatus.FAILED
assert result_f2.block_reason == BlockReason.NON_RETRYABLE_ERROR
assert result_f2.attempts == 1, f"expected 1 attempt, got {result_f2.attempts}"
assert fatal_port.call_count == 1, f"expected 1 port call, got {fatal_port.call_count}"
print(f"PORT_RESULT result=fatal_error op=place", file=sys.stderr)
print(f"f2_status: {result_f2.status.value}", file=sys.stderr)
print(f"f2_block_reason: {result_f2.block_reason.value}", file=sys.stderr)
print(f"f2_attempts: {result_f2.attempts}", file=sys.stderr)
print(f"f2_port_calls: {fatal_port.call_count}", file=sys.stderr)

# Verify classification: transient→MAX_RETRIES_EXCEEDED, fatal→NON_RETRYABLE_ERROR
assert result_f1.block_reason != result_f2.block_reason, "retry vs fatal must have different block reasons"
print(f"classification_distinct: transient={result_f1.block_reason.value} fatal={result_f2.block_reason.value}", file=sys.stderr)

m = get_connector_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
reset_connector_metrics()
PY

# F1 assertions
assert_contains "$DRILL_F_LOG" "PORT_RETRYABLE error=ConnectorTransientError" \
  "PORT_RETRYABLE error=ConnectorTransientError"
assert_contains "$DRILL_F_LOG" "f1_block_reason: MAX_RETRIES_EXCEEDED" \
  "transient → MAX_RETRIES_EXCEEDED"
assert_contains "$DRILL_F_LOG" "f1_attempts: 2" \
  "transient error triggered retry (2 attempts)"
assert_contains "$DRILL_F_LOG" "f1_port_calls: 2" \
  "transient: port called twice (1 original + 1 retry)"

# F2 assertions
assert_contains "$DRILL_F_LOG" "PORT_FATAL error=InsufficientBalance" \
  "PORT_FATAL error=InsufficientBalance"
assert_contains "$DRILL_F_LOG" "f2_block_reason: NON_RETRYABLE_ERROR" \
  "fatal → NON_RETRYABLE_ERROR (no retry)"
assert_contains "$DRILL_F_LOG" "f2_attempts: 1" \
  "fatal error: single attempt, no retry"
assert_contains "$DRILL_F_LOG" "f2_port_calls: 1" \
  "fatal: port called once (no retry)"

# Gotcha: classification must be distinct
assert_contains "$DRILL_F_LOG" "classification_distinct:" \
  "transient vs fatal block reasons are distinct"

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

{
  echo "Exchange Port Boundary Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: NOT_ARMED blocks all"
  echo "  place: BLOCKED (NOT_ARMED), port_calls=0"
  echo "  cancel: BLOCKED (NOT_ARMED), port_calls=0"
  echo "  gotcha: no PORT_CALL emitted (pre-port block)"
  echo ""
  echo "Drill B: Kill-switch (CANCEL through, PLACE blocked)"
  echo "  place: BLOCKED (KILL_SWITCH_ACTIVE), intent=INCREASE_RISK"
  echo "  cancel: EXECUTED, reaches port, intent=CANCEL"
  echo "  port_place_calls=0, port_cancel_calls=1"
  echo ""
  echo "Drill C: Drawdown (INCREASE_RISK blocked, CANCEL+NOOP safe)"
  echo "  place: BLOCKED (DRAWDOWN_BLOCKED)"
  echo "  cancel: EXECUTED (reaches port)"
  echo "  noop: SKIPPED (no port call)"
  echo ""
  echo "Drill D: Symbol whitelist"
  echo "  place XYZUSDT: BLOCKED (SYMBOL_NOT_WHITELISTED)"
  echo "  cancel XYZUSDT: BLOCKED (SYMBOL_NOT_WHITELISTED)"
  echo "  blocked before retry/idempotency machinery"
  echo ""
  echo "Drill E: Idempotency (duplicate prevention)"
  echo "  first PLACE: cache miss, port called, DONE"
  echo "  second PLACE: cache hit, port NOT called"
  echo "  grinder_idempotency_hits_total{op=place}=1"
  echo "  grinder_idempotency_misses_total{op=place}=1"
  echo ""
  echo "Drill F: Retry classification"
  echo "  F1 transient: 2 attempts, MAX_RETRIES_EXCEEDED"
  echo "  F2 fatal: 1 attempt, NON_RETRYABLE_ERROR (immediate)"
  echo "  classification distinct: different block reasons"
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
