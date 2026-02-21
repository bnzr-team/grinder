#!/usr/bin/env bash
# fire_drill_account_sync.sh -- CI-safe AccountSyncer fire drill (Launch-15 PR3).
#
# Deterministically exercises the AccountSyncer with synthetic snapshots:
#   Drill A: Clean sync — happy path, no mismatches, metrics recorded
#   Drill B: Mismatch detection — duplicate_key + negative_qty flagged
#   Drill C: Orphan order — exchange order not tracked by engine
#   Drill D: Timestamp regression — snapshot older than last accepted
#   Drill E: Metrics contract smoke — verify account sync patterns in MetricsBuilder
#
# All drills use the REAL AccountSyncer.sync() — actual prod code path.
# No API keys needed. No network calls. No changes to src/grinder/.
# Takes ~2 seconds (pure CPU, no sleeps).
#
# Usage:
#   bash scripts/fire_drill_account_sync.sh
#
# Evidence artifacts saved under ${GRINDER_ARTIFACT_DIR:-.artifacts}/account_sync_fire_drill/<ts>/
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
EVIDENCE_DIR="${ART_ROOT}/account_sync_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== Account Sync Fire Drill (Launch-15 PR3) ==="
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
# Drill A: Clean sync — happy path (no mismatches, metrics recorded)
# =========================================================================

echo "--- Drill A: Clean sync (happy path, no mismatches) ---"

DRILL_A_METRICS="$EVIDENCE_DIR/drill_a_metrics.txt"
DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"

PYTHONPATH=src python3 - "$DRILL_A_METRICS" <<'PY' 2>"$DRILL_A_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.account.contracts import AccountSnapshot, PositionSnap, OpenOrderSnap
from grinder.account.syncer import AccountSyncer
from grinder.account.metrics import get_account_sync_metrics, reset_account_sync_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

# Reset metrics
reset_account_sync_metrics()

# Build a clean snapshot with 2 positions + 1 open order
snapshot = AccountSnapshot(
    positions=(
        PositionSnap(
            symbol="BTCUSDT", side="LONG", qty=Decimal("1.5"),
            entry_price=Decimal("50000"), mark_price=Decimal("50100"),
            unrealized_pnl=Decimal("150"), leverage=10, ts=1700000000,
        ),
        PositionSnap(
            symbol="ETHUSDT", side="LONG", qty=Decimal("10"),
            entry_price=Decimal("3000"), mark_price=Decimal("3010"),
            unrealized_pnl=Decimal("100"), leverage=5, ts=1700000000,
        ),
    ),
    open_orders=(
        OpenOrderSnap(
            order_id="ord_1", symbol="BTCUSDT", side="BUY",
            order_type="LIMIT", price=Decimal("49000"), qty=Decimal("0.1"),
            filled_qty=Decimal("0"), reduce_only=False, status="NEW",
            ts=1700000000,
        ),
    ),
    ts=1700000000,
    source="fire_drill",
)

# Fake port returning the snapshot
class FakePort:
    def fetch_account_snapshot(self):
        return snapshot
    def place_order(self, **kw):
        return "fake"
    def cancel_order(self, _oid):
        return True
    def replace_order(self, **kw):
        return "fake"
    def fetch_open_orders(self, _sym):
        return []
    def fetch_positions(self):
        return []

port = FakePort()
syncer = AccountSyncer(port)

result = syncer.sync()

print(f"drill_a_ok: {result.ok}", file=sys.stderr)
print(f"drill_a_mismatches: {len(result.mismatches)}", file=sys.stderr)
print(f"drill_a_snapshot_ts: {result.snapshot.ts}", file=sys.stderr)
print(f"drill_a_positions: {len(result.snapshot.positions)}", file=sys.stderr)
print(f"drill_a_open_orders: {len(result.snapshot.open_orders)}", file=sys.stderr)
print(f"drill_a_last_ts: {syncer.last_ts}", file=sys.stderr)

assert result.ok, f"Expected ok=True, got ok={result.ok}"
assert len(result.mismatches) == 0, f"Expected 0 mismatches, got {len(result.mismatches)}"
assert result.snapshot.ts == 1700000000
assert syncer.last_ts == 1700000000

# Verify metrics
m = get_account_sync_metrics()
assert m.last_sync_ts == 1700000000, f"Expected last_sync_ts=1700000000, got {m.last_sync_ts}"
assert m.positions_count == 2, f"Expected positions_count=2, got {m.positions_count}"
assert m.open_orders_count == 1, f"Expected open_orders_count=1, got {m.open_orders_count}"

# Pending notional: 49000 * 0.1 = 4900.0
assert abs(m.pending_notional - 4900.0) < 0.01, \
    f"Expected pending_notional=4900.0, got {m.pending_notional}"

print(f"drill_a_metric_last_sync_ts: {m.last_sync_ts}", file=sys.stderr)
print(f"drill_a_metric_positions_count: {m.positions_count}", file=sys.stderr)
print(f"drill_a_metric_open_orders_count: {m.open_orders_count}", file=sys.stderr)
print(f"drill_a_metric_pending_notional: {m.pending_notional}", file=sys.stderr)

print("drill_a_PROVEN: clean sync, 2 positions + 1 order, metrics recorded", file=sys.stderr)

# Render metrics
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
  "drill_a_ok: True" \
  "clean sync: ok=True"

assert_contains "$DRILL_A_LOG" \
  "drill_a_mismatches: 0" \
  "clean sync: 0 mismatches"

assert_contains "$DRILL_A_LOG" \
  "drill_a_PROVEN: clean sync" \
  "clean sync proven"

assert_contains "$DRILL_A_METRICS" \
  "grinder_account_sync_last_ts 1700000000" \
  "metric: last_sync_ts=1700000000"

assert_contains "$DRILL_A_METRICS" \
  "grinder_account_sync_positions_count 2" \
  "metric: positions_count=2"

assert_contains "$DRILL_A_METRICS" \
  "grinder_account_sync_open_orders_count 1" \
  "metric: open_orders_count=1"

assert_contains "$DRILL_A_METRICS" \
  "grinder_account_sync_pending_notional 4900.00" \
  "metric: pending_notional=4900.00"

echo ""

# =========================================================================
# Drill B: Mismatch detection — duplicate_key + negative_qty
# =========================================================================

echo "--- Drill B: Mismatch detection (duplicate_key + negative_qty) ---"

DRILL_B_METRICS="$EVIDENCE_DIR/drill_b_metrics.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"

PYTHONPATH=src python3 - "$DRILL_B_METRICS" <<'PY' 2>"$DRILL_B_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.account.contracts import AccountSnapshot, PositionSnap, OpenOrderSnap
from grinder.account.syncer import AccountSyncer
from grinder.account.metrics import get_account_sync_metrics, reset_account_sync_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

reset_account_sync_metrics()

# Snapshot with duplicate position key AND negative order qty
snapshot = AccountSnapshot(
    positions=(
        PositionSnap(
            symbol="BTCUSDT", side="LONG", qty=Decimal("1.5"),
            entry_price=Decimal("50000"), mark_price=Decimal("50100"),
            unrealized_pnl=Decimal("150"), leverage=10, ts=1700001000,
        ),
        PositionSnap(
            symbol="BTCUSDT", side="LONG", qty=Decimal("2.0"),
            entry_price=Decimal("51000"), mark_price=Decimal("51100"),
            unrealized_pnl=Decimal("200"), leverage=10, ts=1700001000,
        ),
    ),
    open_orders=(
        OpenOrderSnap(
            order_id="ord_neg", symbol="BTCUSDT", side="BUY",
            order_type="LIMIT", price=Decimal("49000"), qty=Decimal("-0.5"),
            filled_qty=Decimal("0"), reduce_only=False, status="NEW",
            ts=1700001000,
        ),
    ),
    ts=1700001000,
    source="fire_drill",
)

class FakePort:
    def fetch_account_snapshot(self):
        return snapshot
    def place_order(self, **kw):
        return "fake"
    def cancel_order(self, _oid):
        return True
    def replace_order(self, **kw):
        return "fake"
    def fetch_open_orders(self, _sym):
        return []
    def fetch_positions(self):
        return []

port = FakePort()
syncer = AccountSyncer(port)

result = syncer.sync()

print(f"drill_b_ok: {result.ok}", file=sys.stderr)
print(f"drill_b_mismatch_count: {len(result.mismatches)}", file=sys.stderr)

rules = [m.rule for m in result.mismatches]
print(f"drill_b_rules: {sorted(rules)}", file=sys.stderr)

for m in result.mismatches:
    print(f"drill_b_mismatch: rule={m.rule} detail={m.detail}", file=sys.stderr)

assert not result.ok, f"Expected ok=False, got ok={result.ok}"
assert "duplicate_key" in rules, f"Expected duplicate_key in rules, got {rules}"
assert "negative_qty" in rules, f"Expected negative_qty in rules, got {rules}"
assert len(result.mismatches) == 2, f"Expected 2 mismatches, got {len(result.mismatches)}"

# Verify mismatch metrics
m = get_account_sync_metrics()
assert m.mismatches.get("duplicate_key", 0) == 1
assert m.mismatches.get("negative_qty", 0) == 1

print(f"drill_b_metric_mismatches: {dict(m.mismatches)}", file=sys.stderr)

print("drill_b_PROVEN: duplicate_key + negative_qty detected, ok=False", file=sys.stderr)

# Render metrics
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
  "drill_b_ok: False" \
  "mismatch sync: ok=False"

assert_contains "$DRILL_B_LOG" \
  "drill_b_mismatch_count: 2" \
  "mismatch sync: 2 mismatches"

assert_contains "$DRILL_B_LOG" \
  "drill_b_PROVEN: duplicate_key + negative_qty detected" \
  "duplicate_key + negative_qty proven"

assert_contains "$DRILL_B_LOG" \
  "drill_b_mismatch: rule=duplicate_key" \
  "mismatch rule=duplicate_key present"

assert_contains "$DRILL_B_LOG" \
  "drill_b_mismatch: rule=negative_qty" \
  "mismatch rule=negative_qty present"

assert_contains "$DRILL_B_METRICS" \
  'grinder_account_sync_mismatches_total{rule="duplicate_key"} 1' \
  "metric: mismatches_total{rule=duplicate_key}=1"

assert_contains "$DRILL_B_METRICS" \
  'grinder_account_sync_mismatches_total{rule="negative_qty"} 1' \
  "metric: mismatches_total{rule=negative_qty}=1"

echo ""

# =========================================================================
# Drill C: Orphan order — exchange order not tracked by engine
# =========================================================================

echo "--- Drill C: Orphan order (exchange order not in known_order_ids) ---"

DRILL_C_METRICS="$EVIDENCE_DIR/drill_c_metrics.txt"
DRILL_C_LOG="$EVIDENCE_DIR/drill_c_log.txt"

PYTHONPATH=src python3 - "$DRILL_C_METRICS" <<'PY' 2>"$DRILL_C_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.account.contracts import AccountSnapshot, PositionSnap, OpenOrderSnap
from grinder.account.syncer import AccountSyncer
from grinder.account.metrics import get_account_sync_metrics, reset_account_sync_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

reset_account_sync_metrics()

# Snapshot with 2 orders: one known, one orphan
snapshot = AccountSnapshot(
    positions=(),
    open_orders=(
        OpenOrderSnap(
            order_id="known_ord_1", symbol="BTCUSDT", side="BUY",
            order_type="LIMIT", price=Decimal("49000"), qty=Decimal("0.01"),
            filled_qty=Decimal("0"), reduce_only=False, status="NEW",
            ts=1700002000,
        ),
        OpenOrderSnap(
            order_id="orphan_ord_x", symbol="BTCUSDT", side="SELL",
            order_type="LIMIT", price=Decimal("55000"), qty=Decimal("0.05"),
            filled_qty=Decimal("0"), reduce_only=False, status="NEW",
            ts=1700002000,
        ),
    ),
    ts=1700002000,
    source="fire_drill",
)

class FakePort:
    def fetch_account_snapshot(self):
        return snapshot
    def place_order(self, **kw):
        return "fake"
    def cancel_order(self, _oid):
        return True
    def replace_order(self, **kw):
        return "fake"
    def fetch_open_orders(self, _sym):
        return []
    def fetch_positions(self):
        return []

port = FakePort()
syncer = AccountSyncer(port)

# Pass known_order_ids — "orphan_ord_x" is NOT in the set
result = syncer.sync(known_order_ids=frozenset({"known_ord_1", "internal_ord_2"}))

print(f"drill_c_ok: {result.ok}", file=sys.stderr)
print(f"drill_c_mismatch_count: {len(result.mismatches)}", file=sys.stderr)

rules = [m.rule for m in result.mismatches]
print(f"drill_c_rules: {rules}", file=sys.stderr)

for m in result.mismatches:
    print(f"drill_c_mismatch: rule={m.rule} detail={m.detail}", file=sys.stderr)

assert not result.ok, f"Expected ok=False, got {result.ok}"
assert "orphan_order" in rules, f"Expected orphan_order in rules, got {rules}"
assert len(result.mismatches) == 1, f"Expected 1 mismatch, got {len(result.mismatches)}"

# Verify "known_ord_1" was NOT flagged
orphan_details = [m.detail for m in result.mismatches if m.rule == "orphan_order"]
assert "orphan_ord_x" in orphan_details[0], \
    f"Expected orphan_ord_x in detail, got {orphan_details}"
assert "known_ord_1" not in str(orphan_details), \
    "known_ord_1 should NOT be flagged as orphan"

# Metrics
m = get_account_sync_metrics()
assert m.mismatches.get("orphan_order", 0) == 1

print(f"drill_c_metric_mismatches: {dict(m.mismatches)}", file=sys.stderr)

print("drill_c_PROVEN: orphan_ord_x flagged, known_ord_1 not flagged", file=sys.stderr)

# Render metrics
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
  "drill_c_ok: False" \
  "orphan sync: ok=False"

assert_contains "$DRILL_C_LOG" \
  "drill_c_mismatch_count: 1" \
  "orphan sync: 1 mismatch"

assert_contains "$DRILL_C_LOG" \
  "drill_c_PROVEN: orphan_ord_x flagged, known_ord_1 not flagged" \
  "orphan order proven"

assert_contains "$DRILL_C_LOG" \
  "drill_c_mismatch: rule=orphan_order" \
  "mismatch rule=orphan_order present"

assert_contains "$DRILL_C_METRICS" \
  'grinder_account_sync_mismatches_total{rule="orphan_order"} 1' \
  "metric: mismatches_total{rule=orphan_order}=1"

echo ""

# =========================================================================
# Drill D: Timestamp regression — snapshot older than last accepted
# =========================================================================

echo "--- Drill D: Timestamp regression (snapshot.ts < last_ts) ---"

DRILL_D_METRICS="$EVIDENCE_DIR/drill_d_metrics.txt"
DRILL_D_LOG="$EVIDENCE_DIR/drill_d_log.txt"

PYTHONPATH=src python3 - "$DRILL_D_METRICS" <<'PY' 2>"$DRILL_D_LOG"
import sys
import logging
from decimal import Decimal

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

from grinder.account.contracts import AccountSnapshot, PositionSnap, OpenOrderSnap
from grinder.account.syncer import AccountSyncer
from grinder.account.metrics import get_account_sync_metrics, reset_account_sync_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)

metrics_path = sys.argv[1]

reset_account_sync_metrics()

# First snapshot at ts=5000
snap1 = AccountSnapshot(
    positions=(),
    open_orders=(),
    ts=5000,
    source="fire_drill",
)

# Second snapshot at ts=3000 (OLDER — regression)
snap2 = AccountSnapshot(
    positions=(),
    open_orders=(),
    ts=3000,
    source="fire_drill",
)

call_count = [0]

class FakePort:
    def fetch_account_snapshot(self):
        call_count[0] += 1
        if call_count[0] == 1:
            return snap1
        return snap2
    def place_order(self, **kw):
        return "fake"
    def cancel_order(self, _oid):
        return True
    def replace_order(self, **kw):
        return "fake"
    def fetch_open_orders(self, _sym):
        return []
    def fetch_positions(self):
        return []

port = FakePort()
syncer = AccountSyncer(port)

# First sync: establishes last_ts=5000
result1 = syncer.sync()
print(f"drill_d_sync1_ok: {result1.ok}", file=sys.stderr)
print(f"drill_d_sync1_last_ts: {syncer.last_ts}", file=sys.stderr)
assert result1.ok
assert syncer.last_ts == 5000

# Second sync: ts=3000 < last_ts=5000 -> ts_regression
result2 = syncer.sync()
rules = [m.rule for m in result2.mismatches]
print(f"drill_d_sync2_ok: {result2.ok}", file=sys.stderr)
print(f"drill_d_sync2_rules: {rules}", file=sys.stderr)
print(f"drill_d_sync2_last_ts: {syncer.last_ts}", file=sys.stderr)

for m in result2.mismatches:
    print(f"drill_d_mismatch: rule={m.rule} detail={m.detail}", file=sys.stderr)

assert not result2.ok, f"Expected ok=False, got {result2.ok}"
assert "ts_regression" in rules, f"Expected ts_regression in rules, got {rules}"

# Verify last_ts NOT updated (regression rejected)
assert syncer.last_ts == 5000, \
    f"Expected last_ts=5000 (unchanged), got {syncer.last_ts}"

# Metrics
m = get_account_sync_metrics()
assert m.mismatches.get("ts_regression", 0) == 1

print(f"drill_d_metric_mismatches: {dict(m.mismatches)}", file=sys.stderr)

print("drill_d_PROVEN: ts_regression detected, last_ts unchanged (5000)", file=sys.stderr)

# Render metrics
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

# Shell assertions for Drill D
assert_contains "$DRILL_D_LOG" \
  "drill_d_sync1_ok: True" \
  "first sync ok=True (establishes last_ts)"

assert_contains "$DRILL_D_LOG" \
  "drill_d_sync2_ok: False" \
  "regression sync: ok=False"

assert_contains "$DRILL_D_LOG" \
  "drill_d_sync2_last_ts: 5000" \
  "last_ts unchanged at 5000 (regression rejected)"

assert_contains "$DRILL_D_LOG" \
  "drill_d_PROVEN: ts_regression detected" \
  "ts_regression proven"

assert_contains "$DRILL_D_LOG" \
  "drill_d_mismatch: rule=ts_regression" \
  "mismatch rule=ts_regression present"

assert_contains "$DRILL_D_METRICS" \
  'grinder_account_sync_mismatches_total{rule="ts_regression"} 1' \
  "metric: mismatches_total{rule=ts_regression}=1"

echo ""

# =========================================================================
# Drill E: Metrics contract smoke — all account sync patterns in MetricsBuilder
# =========================================================================

echo "--- Drill E: Metrics contract smoke (account sync patterns in MetricsBuilder) ---"

DRILL_E_METRICS="$EVIDENCE_DIR/drill_e_metrics.txt"

PYTHONPATH=src python3 - "$DRILL_E_METRICS" <<'PY' 2>&1
import sys
from decimal import Decimal

from grinder.account.metrics import get_account_sync_metrics, reset_account_sync_metrics
from grinder.observability.metrics_builder import (
    MetricsBuilder, RiskMetricsState, set_risk_metrics_state, reset_risk_metrics_state,
)
from grinder.observability.live_contract import REQUIRED_METRICS_PATTERNS

metrics_path = sys.argv[1]

# Reset and seed account sync metrics with realistic data
reset_account_sync_metrics()
m = get_account_sync_metrics()
m.record_sync(ts=1700000000, positions=2, open_orders=3, pending_notional=5000.0)
m.record_mismatch("duplicate_key")

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

# Check all account_sync patterns from REQUIRED_METRICS_PATTERNS
acct_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "account_sync" in p.lower()]
print(f"drill_e_pattern_count: {len(acct_patterns)}")

missing = []
for pattern in acct_patterns:
    if pattern not in full_output:
        missing.append(pattern)

if missing:
    print(f"drill_e_FAIL: {len(missing)} account sync patterns missing:")
    for p in missing:
        print(f"  MISSING: {p}")
    sys.exit(1)
else:
    print(f"drill_e_PROVEN: all {len(acct_patterns)} account sync patterns present in MetricsBuilder output")
PY

DRILL_E_RC=$?
if [[ "$DRILL_E_RC" -ne 0 ]]; then
  fail "Drill E: metrics contract smoke FAILED"
else
  pass "Drill E: all account sync patterns present in MetricsBuilder output"
fi

# Verify specific metric lines in the metrics file
assert_contains "$DRILL_E_METRICS" \
  "# HELP grinder_account_sync_last_ts" \
  "metric: HELP grinder_account_sync_last_ts present"

assert_contains "$DRILL_E_METRICS" \
  "# TYPE grinder_account_sync_last_ts" \
  "metric: TYPE grinder_account_sync_last_ts present"

assert_contains "$DRILL_E_METRICS" \
  "grinder_account_sync_last_ts 1700000000" \
  "metric: last_ts value present"

assert_contains "$DRILL_E_METRICS" \
  "# HELP grinder_account_sync_mismatches_total" \
  "metric: HELP grinder_account_sync_mismatches_total present"

assert_contains "$DRILL_E_METRICS" \
  "# TYPE grinder_account_sync_mismatches_total" \
  "metric: TYPE grinder_account_sync_mismatches_total present"

assert_contains "$DRILL_E_METRICS" \
  "grinder_account_sync_positions_count 2" \
  "metric: positions_count=2 present"

assert_contains "$DRILL_E_METRICS" \
  "grinder_account_sync_pending_notional 5000.00" \
  "metric: pending_notional=5000.00 present"

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

DRILL_A_TS_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_account_sync_last_ts')"
DRILL_A_POS_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_account_sync_positions_count')"
DRILL_A_ORD_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_account_sync_open_orders_count')"
DRILL_A_NOT_LINE="$(get_metric_line "$DRILL_A_METRICS" 'grinder_account_sync_pending_notional')"
DRILL_B_DUP_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_account_sync_mismatches_total{rule="duplicate_key"}')"
DRILL_B_NEG_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_account_sync_mismatches_total{rule="negative_qty"}')"
DRILL_C_ORPH_LINE="$(get_metric_line "$DRILL_C_METRICS" 'grinder_account_sync_mismatches_total{rule="orphan_order"}')"
DRILL_D_TS_LINE="$(get_metric_line "$DRILL_D_METRICS" 'grinder_account_sync_mismatches_total{rule="ts_regression"}')"

{
  echo "Account Sync Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: Clean sync (happy path)"
  echo "  snapshot: 2 positions + 1 open order, ts=1700000000, source=fire_drill"
  echo "  result: ok=True, 0 mismatches, last_ts updated"
  echo "  metric: ${DRILL_A_TS_LINE}"
  echo "  metric: ${DRILL_A_POS_LINE}"
  echo "  metric: ${DRILL_A_ORD_LINE}"
  echo "  metric: ${DRILL_A_NOT_LINE}"
  echo "  code_path: AccountSyncer.sync() -> _detect_mismatches() (syncer.py)"
  echo ""
  echo "Drill B: Mismatch detection (duplicate_key + negative_qty)"
  echo "  snapshot: 2 positions with same (BTCUSDT, LONG) + 1 order with qty=-0.5"
  echo "  result: ok=False, 2 mismatches"
  echo "  rules: duplicate_key, negative_qty"
  echo "  metric: ${DRILL_B_DUP_LINE}"
  echo "  metric: ${DRILL_B_NEG_LINE}"
  echo ""
  echo "Drill C: Orphan order"
  echo "  snapshot: 2 open orders (known_ord_1 + orphan_ord_x)"
  echo "  known_order_ids: {known_ord_1, internal_ord_2}"
  echo "  result: ok=False, 1 mismatch (orphan_ord_x flagged, known_ord_1 not)"
  echo "  metric: ${DRILL_C_ORPH_LINE}"
  echo ""
  echo "Drill D: Timestamp regression"
  echo "  sync 1: ts=5000 (accepted, last_ts=5000)"
  echo "  sync 2: ts=3000 (regression: 3000 < 5000)"
  echo "  result: ok=False, ts_regression detected, last_ts unchanged (5000)"
  echo "  metric: ${DRILL_D_TS_LINE}"
  echo ""
  echo "Drill E: Metrics contract smoke"
  echo "  verified: all account sync patterns from REQUIRED_METRICS_PATTERNS present in MetricsBuilder output"
  echo ""
  echo "NOTE: All drills use the REAL AccountSyncer.sync() code path."
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
