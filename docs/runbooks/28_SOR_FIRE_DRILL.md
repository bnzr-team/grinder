# Runbook 28: SOR Fire Drill (Launch-14)

Verifies SmartOrderRouter (SOR) decision paths end-to-end: CANCEL_REPLACE, BLOCK, and NOOP.
Produces timestamped evidence artifacts with integrity checksums.

**SSOT:** `docs/14_SMART_ORDER_ROUTER_SPEC.md`

---

## 1. Overview

The SmartOrderRouter sits after all safety gates (armed, mode, kill-switch, whitelist, drawdown, FSM) and before execution. It decides whether to:

- **CANCEL_REPLACE** — proceed with normal cancel+place (default when no existing order)
- **BLOCK** — reject the action (e.g. price would cross the spread, filter violation)
- **NOOP** — skip the action (e.g. rate-limit budget exhausted)

The fire drill exercises all 3 paths deterministically with synthetic data, proving the wiring works end-to-end through `LiveEngineV0.process_snapshot()`.

---

## 2. Enablement

SOR is **off by default** (safe-by-default). To enable:

**Option A: Config field**
```python
config = LiveEngineConfig(
    sor_enabled=True,  # default: False
    # ... other fields
)
```

**Option B: Environment variable**
```bash
export GRINDER_SOR_ENABLED=1  # truthy: 1, true, yes, on
```

Both require `exchange_filters` to be provided and at least one snapshot to be processed. If either is missing, SOR is silently skipped (debug log emitted).

---

## 3. Prerequisites

- Python environment with grinder installed (`pip install -e ".[dev]"`)
- No API keys needed
- No network calls
- Runs in ~2 seconds

---

## 4. Running the drill

**Recommended (via ops entrypoint):**
```bash
bash scripts/ops_fill_triage.sh sor-fire-drill
```

**Standalone:**
```bash
bash scripts/fire_drill_sor.sh
```

**Artifact root override:**
```bash
GRINDER_ARTIFACT_DIR=/tmp/my_artifacts bash scripts/fire_drill_sor.sh
```

---

## 5. Expected output

```
=== SOR Fire Drill (Launch-14 PR3) ===
evidence_dir: .artifacts/sor_fire_drill/<ts>

--- Drill A: CANCEL_REPLACE (happy path, BUY near market) ---
  PASS: PLACE reaches port (status=EXECUTED)
  PASS: CANCEL_REPLACE proven (port.place_order called)
  PASS: SOR metric recorded (CANCEL_REPLACE)
  PASS: metric: router_decision_total CANCEL_REPLACE in Prometheus output

--- Drill B: BLOCK (spread crossing, ROUTER_BLOCKED) ---
  PASS: BLOCK: status=BLOCKED, block_reason=ROUTER_BLOCKED, 0 port calls
  PASS: BLOCK spread crossing proven
  PASS: SOR metric recorded (BLOCK)
  PASS: metric: router_decision_total BLOCK in Prometheus output

--- Drill C: NOOP (budget exhausted, router-only) ---
  PASS: Drill C is router-only (explicit marker)
  PASS: NOOP: decision=NOOP, reason=RATE_LIMIT_THROTTLE
  PASS: NOOP budget exhausted proven
  PASS: metric: router_decision_total NOOP in Prometheus output

--- Drill D: Metrics contract smoke ---
  PASS: Drill D: all SOR patterns present in MetricsBuilder output
  PASS: metric: HELP/TYPE/series for all SOR patterns

=== Results: 20 passed, 0 failed, 0 skipped ===
```

---

## 6. Verifying artifacts

```bash
# Navigate to the evidence directory printed in output
cd .artifacts/sor_fire_drill/<ts>/

# Verify integrity
sha256sum -c sha256sums.txt

# Review evidence
cat summary.txt
```

Expected `sha256sum -c` output: all lines end with `: OK`.

---

## 7. Interpreting decisions

| Decision | Meaning | When it fires |
|----------|---------|---------------|
| CANCEL_REPLACE | Proceed with normal execution | `existing=None` (no order state tracking yet) |
| BLOCK | Reject the action | Price crosses spread, filter violation (tick/step/min_qty/min_notional) |
| NOOP | Skip the action | Rate-limit budget exhausted |
| AMEND | (deferred) | Never reachable with `existing=None`; normalized to CANCEL_REPLACE if encountered |

---

## 8. Troubleshooting

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| SOR not firing (no decision metrics) | Feature flag OFF | Set `sor_enabled=True` or `GRINDER_SOR_ENABLED=1` |
| SOR skipped, debug log "exchange_filters missing" | No filters provided | Pass `exchange_filters=ExchangeFilters(...)` to `LiveEngineV0` |
| SOR skipped, debug log "no snapshot available" | No snapshot processed | Ensure `process_snapshot()` called before action processing |
| Drill A fails with BLOCKED | Unexpected spread-crossing | Check bid/ask prices in snapshot match drill expectations |
| Drill B fails with EXECUTED | Router not blocking | Verify price >= best_ask for BUY (spread crossing detection) |
| Drill D missing patterns | MetricsBuilder not wiring SOR | Check `_build_sor_metrics()` in `metrics_builder.py` |

---

## 9. Evidence commands (for PR proof)

```bash
# Run via ops entrypoint and capture
bash scripts/ops_fill_triage.sh sor-fire-drill --no-status | tee /tmp/sor_drill.out

# Extract EVIDENCE_REF
grep '^EVIDENCE_REF ' /tmp/sor_drill.out

# Get evidence dir from output
EVID_DIR=$(grep -oP 'evidence_dir: \K\S+' /tmp/sor_drill.out | tail -1)

# Paste into PR body:
cat "$EVID_DIR/summary.txt"
cat "$EVID_DIR/sha256sums.txt"

# Verify integrity
cd "$EVID_DIR" && sha256sum -c sha256sums.txt
```

---

## References

- [SOR Spec](../14_SMART_ORDER_ROUTER_SPEC.md)
- [Evidence Index](00_EVIDENCE_INDEX.md)
- [Ops Quickstart](00_OPS_QUICKSTART.md)
