#!/usr/bin/env bash
# fire_drill_connector_market_data.sh -- CI-safe market data connector fire drill (Launch-10 PR1).
#
# Deterministically exercises five market data connector paths:
#   Drill A: Malformed L2 payload parsing (L2ParseError rejection)
#   Drill B: Stale timestamp detection (DataQualityEngine staleness)
#   Drill C: Unknown symbol filtering (LiveFeedConfig whitelist)
#   Drill D: Non-monotonic sequence + gap + outlier detection
#   Drill E: Happy path (valid DQ + L2 parse)
#
# No API keys needed. No network calls. No changes to src/grinder/.
# Takes ~2 seconds (pure CPU, no sleeps).
#
# Usage:
#   bash scripts/fire_drill_connector_market_data.sh
#
# Evidence artifacts saved under .artifacts/connector_market_data_fire_drill/<ts>/
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
EVIDENCE_DIR=".artifacts/connector_market_data_fire_drill/${EVIDENCE_TS}"
mkdir -p "$EVIDENCE_DIR"

echo "=== Market Data Connector Fire Drill (Launch-10 PR1) ==="
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
# Drill A: Malformed L2 payload parsing
# =========================================================================

echo "--- Drill A: Malformed L2 payload parsing ---"

DRILL_A_LOG="$EVIDENCE_DIR/drill_a_log.txt"

PYTHONPATH=src python3 - <<'PY' 2>"$DRILL_A_LOG"
import json
import sys

from grinder.replay.l2_snapshot import parse_l2_snapshot_line, L2ParseError

cases = []

# Case 1: Invalid JSON
try:
    parse_l2_snapshot_line("not-json-at-all{")
    cases.append(("invalid_json", "FAIL", "no exception raised"))
except L2ParseError as e:
    cases.append(("invalid_json", "PASS", str(e)))

# Case 2: Missing required field (depth omitted)
try:
    line = json.dumps({
        "type": "l2_snapshot", "v": 0, "ts_ms": 1000,
        "symbol": "BTCUSDT", "venue": "test",
        "bids": [["100", "1"]], "asks": [["101", "1"]]
    })
    parse_l2_snapshot_line(line)
    cases.append(("missing_field", "FAIL", "no exception raised"))
except L2ParseError as e:
    cases.append(("missing_field", "PASS", str(e)))

# Case 3: Unsupported schema version
try:
    line = json.dumps({
        "type": "l2_snapshot", "v": 99, "ts_ms": 1000,
        "symbol": "BTCUSDT", "venue": "test", "depth": 1,
        "bids": [["100", "1"]], "asks": [["101", "1"]]
    })
    parse_l2_snapshot_line(line)
    cases.append(("bad_version", "FAIL", "no exception raised"))
except L2ParseError as e:
    cases.append(("bad_version", "PASS", str(e)))

# Case 4: Zero quantity (invariant violation)
try:
    line = json.dumps({
        "type": "l2_snapshot", "v": 0, "ts_ms": 1000,
        "symbol": "BTCUSDT", "venue": "test", "depth": 1,
        "bids": [["100", "0"]], "asks": [["101", "1"]]
    })
    parse_l2_snapshot_line(line)
    cases.append(("zero_qty", "FAIL", "no exception raised"))
except L2ParseError as e:
    cases.append(("zero_qty", "PASS", str(e)))

# Case 5: Crossed book (best_bid >= best_ask)
try:
    line = json.dumps({
        "type": "l2_snapshot", "v": 0, "ts_ms": 1000,
        "symbol": "BTCUSDT", "venue": "test", "depth": 1,
        "bids": [["102", "1"]], "asks": [["101", "1"]]
    })
    parse_l2_snapshot_line(line)
    cases.append(("crossed_book", "FAIL", "no exception raised"))
except L2ParseError as e:
    cases.append(("crossed_book", "PASS", str(e)))

for name, result, msg in cases:
    print(f"L2ParseError_{name}: {result} ({msg})", file=sys.stderr)
PY

# Assert each L2ParseError case
assert_contains "$DRILL_A_LOG" \
  "L2ParseError_invalid_json: PASS" \
  "L2ParseError rejects invalid JSON"

assert_contains "$DRILL_A_LOG" \
  "L2ParseError_missing_field: PASS" \
  "L2ParseError rejects missing required field"

assert_contains "$DRILL_A_LOG" \
  "L2ParseError_bad_version: PASS" \
  "L2ParseError rejects unsupported schema version"

assert_contains "$DRILL_A_LOG" \
  "L2ParseError_zero_qty: PASS" \
  "L2ParseError rejects zero quantity"

assert_contains "$DRILL_A_LOG" \
  "L2ParseError_crossed_book: PASS" \
  "L2ParseError rejects crossed book"

echo ""

# =========================================================================
# Drill B: Stale timestamp detection
# =========================================================================

echo "--- Drill B: Stale timestamp detection ---"

DRILL_B_METRICS="$EVIDENCE_DIR/drill_b_metrics.txt"
DRILL_B_LOG="$EVIDENCE_DIR/drill_b_log.txt"

PYTHONPATH=src python3 - "$DRILL_B_METRICS" <<'PY' 2>"$DRILL_B_LOG"
import sys

from grinder.data.quality import DataQualityConfig
from grinder.data.quality_engine import DataQualityEngine
from grinder.data.quality_metrics import reset_data_quality_metrics, get_data_quality_metrics

reset_data_quality_metrics()
metrics_path = sys.argv[1]

# Config: default book_ticker staleness threshold = 2000ms
# Unknown streams fall back to stale_book_ticker_ms
config = DataQualityConfig(stale_book_ticker_ms=2000)
engine = DataQualityEngine(config)

# Tick 1: establish baseline (no staleness check — first tick, no prev_ts)
v1 = engine.observe_tick(stream="fire_drill", ts_ms=1000, price=50000.0, now_ms=1000)
assert not v1.stale, "first tick should not be stale (no previous timestamp)"
print(f"tick1: stale={v1.stale} is_ok={v1.is_ok}", file=sys.stderr)

# Tick 2: stale — now_ms far ahead of prev_ts (1000)
# Staleness: is_stale(now_ms=4000, prev_ts=1000, threshold=2000) → 3000 >= 2000 → True
v2 = engine.observe_tick(stream="fire_drill", ts_ms=1100, price=50000.0, now_ms=4000)
assert v2.stale, f"second tick should be stale (now=4000, prev_ts=1000, delta=3000 >= 2000)"
print(f"tick2: stale={v2.stale} is_ok={v2.is_ok}", file=sys.stderr)
print(f"staleness_detected: True", file=sys.stderr)

# Write DQ metrics
m = get_data_quality_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"metrics_written: drill_b_metrics.txt", file=sys.stderr)

reset_data_quality_metrics()
PY

# Assert staleness metric
STALE_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_data_stale_total{stream="fire_drill"}')"
if [[ -n "$STALE_LINE" ]]; then
  STALE_VAL="$(metric_value "$STALE_LINE")"
  if [[ "$STALE_VAL" -ge 1 ]]; then
    pass "grinder_data_stale_total{stream=fire_drill} >= 1 ($STALE_VAL)"
  else
    fail "grinder_data_stale_total{stream=fire_drill} not >= 1 ($STALE_VAL)"
  fi
else
  fail "grinder_data_stale_total{stream=fire_drill} not found in metrics"
fi

assert_contains "$DRILL_B_LOG" \
  "staleness_detected: True" \
  "log: staleness detected"

assert_contains "$DRILL_B_LOG" \
  "tick2: stale=True" \
  "log: verdict.stale=True on stale tick"

echo ""

# =========================================================================
# Drill C: Unknown symbol filtering
# =========================================================================

echo "--- Drill C: Unknown symbol filtering ---"

DRILL_C_LOG="$EVIDENCE_DIR/drill_c_log.txt"

PYTHONPATH=src python3 - <<'PY' 2>"$DRILL_C_LOG"
import sys

from grinder.live.feed import LiveFeedConfig

# Config with explicit symbol whitelist
config = LiveFeedConfig(symbols=["BTCUSDT", "ETHUSDT"])

# Known symbol → allowed
assert config.is_symbol_allowed("BTCUSDT"), "BTCUSDT should be allowed"
print(f"symbol_BTCUSDT: allowed={config.is_symbol_allowed('BTCUSDT')}", file=sys.stderr)

# Unknown symbol → filtered
assert not config.is_symbol_allowed("XYZUSDT"), "XYZUSDT should be filtered"
print(f"symbol_XYZUSDT: allowed={config.is_symbol_allowed('XYZUSDT')}", file=sys.stderr)

# Empty whitelist → all allowed
config_empty = LiveFeedConfig(symbols=[])
assert config_empty.is_symbol_allowed("ANYSYMBOL"), "empty whitelist allows all"
print(f"empty_whitelist_ANYSYMBOL: allowed={config_empty.is_symbol_allowed('ANYSYMBOL')}", file=sys.stderr)

print("symbol_filter_proof: PASS", file=sys.stderr)
PY

assert_contains "$DRILL_C_LOG" \
  "symbol_BTCUSDT: allowed=True" \
  "known symbol BTCUSDT allowed"

assert_contains "$DRILL_C_LOG" \
  "symbol_XYZUSDT: allowed=False" \
  "unknown symbol XYZUSDT filtered"

assert_contains "$DRILL_C_LOG" \
  "empty_whitelist_ANYSYMBOL: allowed=True" \
  "empty whitelist allows all symbols"

echo ""

# =========================================================================
# Drill D: Non-monotonic sequence + gap + outlier detection
# =========================================================================

echo "--- Drill D: Non-monotonic sequence + gap + outlier ---"

DRILL_D_METRICS="$EVIDENCE_DIR/drill_d_metrics.txt"
DRILL_D_LOG="$EVIDENCE_DIR/drill_d_log.txt"

PYTHONPATH=src python3 - "$DRILL_D_METRICS" <<'PY' 2>"$DRILL_D_LOG"
import sys

from grinder.data.quality import DataQualityConfig
from grinder.data.quality_engine import DataQualityEngine
from grinder.data.quality_metrics import reset_data_quality_metrics, get_data_quality_metrics

reset_data_quality_metrics()
metrics_path = sys.argv[1]

config = DataQualityConfig(
    gap_buckets_ms=(500, 2000, 5000),
    price_jump_max_bps=500,
)
engine = DataQualityEngine(config)

# --- Non-monotonic: tick2 timestamp < tick1 timestamp ---
# Tick 1: baseline (first call, no gap/outlier)
v1 = engine.observe_tick(stream="fire_drill", ts_ms=5000, price=50000.0)
print(f"tick1: gap_bucket={v1.gap_bucket} outlier={v1.outlier_kind}", file=sys.stderr)

# Tick 2: backward timestamp (ts_ms=3000 < prev 5000) → gap detector returns None
# GapDetector: gap_ms = 3000 - 5000 = -2000, negative → ignored
v2 = engine.observe_tick(stream="fire_drill", ts_ms=3000, price=50000.0)
assert v2.gap_bucket is None, f"negative gap should be ignored, got {v2.gap_bucket}"
print(f"tick2_backward: gap_bucket={v2.gap_bucket} (negative gap ignored)", file=sys.stderr)

# --- Large gap: tick3 far ahead → bucket ">5000" ---
# GapDetector._last_ts is now 3000 (updated even for backward ticks)
# tick3 at 10000 → gap = 10000 - 3000 = 7000 >= 5000 → bucket ">5000"
v3 = engine.observe_tick(stream="fire_drill", ts_ms=10000, price=50000.0)
assert v3.gap_bucket == ">5000", f"expected gap bucket >5000, got {v3.gap_bucket}"
print(f"tick3_large_gap: gap_bucket={v3.gap_bucket}", file=sys.stderr)

# --- Outlier: price jump > 500 bps ---
# Previous price = 50000.0. Jump to 55000.0
# delta_bps = abs(55000 - 50000) / 50000 * 10000 = 1000 > 500 → outlier
v4 = engine.observe_tick(stream="fire_drill", ts_ms=10500, price=55000.0)
assert v4.outlier_kind == "price", f"expected outlier kind 'price', got {v4.outlier_kind}"
print(f"tick4_outlier: outlier_kind={v4.outlier_kind}", file=sys.stderr)
print("non_monotonic_gap_outlier_proof: PASS", file=sys.stderr)

# Write DQ metrics
m = get_data_quality_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"metrics_written: drill_d_metrics.txt", file=sys.stderr)

reset_data_quality_metrics()
PY

# Assert gap metric (>5000 bucket from tick3)
GAP_LINE="$(get_metric_line "$DRILL_D_METRICS" 'grinder_data_gap_total{stream="fire_drill",bucket=">5000"}')"
if [[ -n "$GAP_LINE" ]]; then
  GAP_VAL="$(metric_value "$GAP_LINE")"
  if [[ "$GAP_VAL" -ge 1 ]]; then
    pass "grinder_data_gap_total{bucket=>5000} >= 1 ($GAP_VAL)"
  else
    fail "grinder_data_gap_total{bucket=>5000} not >= 1 ($GAP_VAL)"
  fi
else
  fail "grinder_data_gap_total{bucket=>5000} not found"
fi

# Assert outlier metric
OUTLIER_LINE="$(get_metric_line "$DRILL_D_METRICS" 'grinder_data_outlier_total{stream="fire_drill",kind="price"}')"
if [[ -n "$OUTLIER_LINE" ]]; then
  OUT_VAL="$(metric_value "$OUTLIER_LINE")"
  if [[ "$OUT_VAL" -ge 1 ]]; then
    pass "grinder_data_outlier_total{kind=price} >= 1 ($OUT_VAL)"
  else
    fail "grinder_data_outlier_total{kind=price} not >= 1 ($OUT_VAL)"
  fi
else
  fail "grinder_data_outlier_total{kind=price} not found"
fi

assert_contains "$DRILL_D_LOG" \
  "tick2_backward: gap_bucket=None (negative gap ignored)" \
  "log: negative gap ignored for non-monotonic tick"

assert_contains "$DRILL_D_LOG" \
  "tick3_large_gap: gap_bucket=>5000" \
  "log: large gap classified as >5000"

assert_contains "$DRILL_D_LOG" \
  "tick4_outlier: outlier_kind=price" \
  "log: price outlier detected"

echo ""

# =========================================================================
# Drill E: Happy path (valid DQ + L2 parse)
# =========================================================================

echo "--- Drill E: Happy path ---"

DRILL_E_METRICS="$EVIDENCE_DIR/drill_e_metrics.txt"
DRILL_E_LOG="$EVIDENCE_DIR/drill_e_log.txt"

PYTHONPATH=src python3 - "$DRILL_E_METRICS" <<'PY' 2>"$DRILL_E_LOG"
import json
import sys
from decimal import Decimal

from grinder.data.quality import DataQualityConfig
from grinder.data.quality_engine import DataQualityEngine
from grinder.data.quality_metrics import reset_data_quality_metrics, get_data_quality_metrics
from grinder.replay.l2_snapshot import parse_l2_snapshot_line

reset_data_quality_metrics()
metrics_path = sys.argv[1]

# --- DQ happy path: normal ticks, no anomalies ---
config = DataQualityConfig(
    stale_book_ticker_ms=2000,
    gap_buckets_ms=(500, 2000, 5000),
    price_jump_max_bps=500,
)
engine = DataQualityEngine(config)

# Tick 1: establish baseline
v1 = engine.observe_tick(stream="fire_drill", ts_ms=1000, price=50000.0, now_ms=1000)
print(f"tick1: is_ok={v1.is_ok} stale={v1.stale} gap={v1.gap_bucket} outlier={v1.outlier_kind}", file=sys.stderr)

# Tick 2: normal (small gap, small price change, not stale)
# gap = 200ms < 500ms bucket → no gap event
# price delta = |50050 - 50000| / 50000 * 10000 = 10 bps < 500 → no outlier
# staleness: now_ms - prev_ts = 1200 - 1000 = 200 < 2000 → not stale
v2 = engine.observe_tick(stream="fire_drill", ts_ms=1200, price=50050.0, now_ms=1200)
assert v2.is_ok, f"expected clean verdict, got stale={v2.stale} gap={v2.gap_bucket} outlier={v2.outlier_kind}"
print(f"tick2: is_ok={v2.is_ok} stale={v2.stale} gap={v2.gap_bucket} outlier={v2.outlier_kind}", file=sys.stderr)
print(f"happy_dq_verdict: is_ok=True", file=sys.stderr)

# --- L2 happy path: valid snapshot parses successfully ---
l2_line = json.dumps({
    "type": "l2_snapshot", "v": 0, "ts_ms": 1700000000000,
    "symbol": "BTCUSDT", "venue": "binance_futures_usdtm", "depth": 2,
    "bids": [["50000.00", "1.5"], ["49999.00", "2.0"]],
    "asks": [["50001.00", "1.0"], ["50002.00", "3.0"]],
    "meta": {"source": "fire_drill"},
})
snap = parse_l2_snapshot_line(l2_line)
assert snap.symbol == "BTCUSDT", f"symbol mismatch: {snap.symbol}"
assert snap.depth == 2, f"depth mismatch: {snap.depth}"
assert snap.bids[0].price == Decimal("50000.00"), f"bid price: {snap.bids[0].price}"
assert snap.asks[0].price == Decimal("50001.00"), f"ask price: {snap.asks[0].price}"
assert snap.best_bid == Decimal("50000.00"), f"best_bid: {snap.best_bid}"
assert snap.best_ask == Decimal("50001.00"), f"best_ask: {snap.best_ask}"
assert snap.best_bid < snap.best_ask, "book should not be crossed"
print(f"l2_parse: symbol={snap.symbol} depth={snap.depth} best_bid={snap.best_bid} best_ask={snap.best_ask}", file=sys.stderr)
print(f"l2_parse_valid: PASS", file=sys.stderr)

# Write DQ metrics (should show zero-value placeholders — no anomalies triggered)
m = get_data_quality_metrics()
lines = m.to_prometheus_lines()
with open(metrics_path, "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"metrics_written: drill_e_metrics.txt", file=sys.stderr)

reset_data_quality_metrics()
PY

# Assert happy DQ verdict
assert_contains "$DRILL_E_LOG" \
  "happy_dq_verdict: is_ok=True" \
  "DQ verdict is clean (is_ok=True)"

# Assert valid L2 parse
assert_contains "$DRILL_E_LOG" \
  "l2_parse_valid: PASS" \
  "valid L2 snapshot parses successfully"

assert_contains "$DRILL_E_LOG" \
  "l2_parse: symbol=BTCUSDT depth=2 best_bid=50000.00 best_ask=50001.00" \
  "L2 snapshot fields correct"

# Assert no anomalies in happy-path metrics (zero-value placeholders)
STALE_E="$(get_metric_line "$DRILL_E_METRICS" 'grinder_data_stale_total')"
if echo "$STALE_E" | grep -qF 'stream="none"'; then
  pass "no staleness events in happy path (zero placeholder)"
elif [[ -n "$STALE_E" ]]; then
  STALE_E_VAL="$(metric_value "$STALE_E")"
  if [[ "$STALE_E_VAL" -eq 0 ]]; then
    pass "no staleness events in happy path (count=0)"
  else
    fail "unexpected staleness in happy path ($STALE_E)"
  fi
else
  fail "grinder_data_stale_total missing from happy-path metrics"
fi

echo ""

# =========================================================================
# Evidence summary
# =========================================================================

DRILL_B_STALE_LINE="$(get_metric_line "$DRILL_B_METRICS" 'grinder_data_stale_total{stream="fire_drill"}')"
DRILL_D_GAP_LINE="$(get_metric_line "$DRILL_D_METRICS" 'grinder_data_gap_total{stream="fire_drill",bucket=">5000"}')"
DRILL_D_OUTLIER_LINE="$(get_metric_line "$DRILL_D_METRICS" 'grinder_data_outlier_total{stream="fire_drill",kind="price"}')"

{
  echo "Market Data Connector Fire Drill Evidence"
  echo "evidence_dir: ${EVIDENCE_DIR}"
  echo ""
  echo "Drill A: Malformed L2 payload parsing"
  echo "  rejected: invalid_json, missing_field, bad_version, zero_qty, crossed_book"
  echo "  error_type: L2ParseError (all 5 cases)"
  echo ""
  echo "Drill B: Stale timestamp detection"
  echo "  metric: ${DRILL_B_STALE_LINE}"
  echo "  mechanism: is_stale(now_ms=4000, prev_ts=1000, threshold=2000) -> True"
  echo "  verdict: stale=True"
  echo ""
  echo "Drill C: Unknown symbol filtering"
  echo "  BTCUSDT: allowed=True (in whitelist)"
  echo "  XYZUSDT: allowed=False (not in whitelist)"
  echo "  empty_whitelist: allowed=True (all pass)"
  echo ""
  echo "Drill D: Non-monotonic sequence + gap + outlier"
  echo "  metric: ${DRILL_D_GAP_LINE}"
  echo "  metric: ${DRILL_D_OUTLIER_LINE}"
  echo "  non-monotonic: gap_bucket=None (negative gap ignored)"
  echo "  large_gap: bucket=>5000 (7000ms gap)"
  echo "  outlier: kind=price (1000 bps > 500 bps threshold)"
  echo ""
  echo "Drill E: Happy path"
  echo "  DQ: verdict.is_ok=True (no anomalies)"
  echo "  L2: parsed symbol=BTCUSDT depth=2 best_bid=50000.00 best_ask=50001.00"
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
