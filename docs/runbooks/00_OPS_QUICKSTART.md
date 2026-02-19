# Runbook 00: Ops Quickstart (Launch-07)

Quick-reference for operators: from "alert fired" to "evidence pasted" in under 2 minutes.

See also: [Evidence Index](00_EVIDENCE_INDEX.md) | [Fill Tracker Triage](26_FILL_TRACKER_TRIAGE.md)

---

## One-command triage

The fastest path: use the triage wrapper. It runs the right script, surfaces the evidence directory, and tells you what to do next.

```bash
bash scripts/ops_fill_triage.sh <mode>
```

| Mode | What it runs | API keys | Runtime |
|------|-------------|----------|---------|
| `local` | `smoke_fill_ingest.sh` (FakePort) | No | ~5s |
| `staging` | `smoke_fill_ingest_staging.sh` (Gate A always; B/C if creds) | Yes (Gate B/C) | ~2 min |
| `fire-drill` | `fire_drill_fill_alerts.sh` (alert input proof) | No | ~10s |

```bash
# Examples
bash scripts/ops_fill_triage.sh local        # quick wiring check
bash scripts/ops_fill_triage.sh fire-drill   # prove alert inputs
bash scripts/ops_fill_triage.sh staging      # full staging evidence
bash scripts/ops_fill_triage.sh -h           # help
```

The wrapper does **not** change the underlying script output or evidence format. It runs the script, shows its output live, then prints where artifacts landed and what to do next.

### Risk triage (one command)

```bash
bash scripts/ops_risk_triage.sh <mode>
```

| Mode | What it runs | API keys | Runtime |
|------|-------------|----------|---------|
| `killswitch-drawdown` | `fire_drill_risk_killswitch_drawdown.sh` | No | ~2s |
| `budget-limits` | `fire_drill_reconcile_budget_limits.sh` | No | ~2s |

```bash
# Examples
bash scripts/ops_risk_triage.sh killswitch-drawdown  # kill-switch + drawdown proof
bash scripts/ops_risk_triage.sh budget-limits        # budget enforcement proof
bash scripts/ops_risk_triage.sh -h                   # help
```

---

## What to run first (without wrapper)

You can also run the scripts directly:

| Situation | Script | API keys | Runtime |
|-----------|--------|----------|---------|
| Local dev / CI -- no credentials | `bash scripts/smoke_fill_ingest.sh` | No | ~5s |
| Staging -- verify real Binance reads + cursor persistence | `bash scripts/smoke_fill_ingest_staging.sh` | Yes (Gate B/C) | ~2 min |
| Alert-input verification -- prove alert signals are produced | `bash scripts/fire_drill_fill_alerts.sh` | No | ~10s |

**Decision tree:**

1. No API keys or just checking wiring? Run `local`.
2. Have staging credentials and need end-to-end proof? Run `staging`.
3. Alert fired and you need to verify the inputs are real? Run `fire-drill`.

All scripts exit non-zero on failure and print `PASS`/`FAIL` per check.

---

## Evidence basics

### Where artifacts live

All evidence scripts write to `.artifacts/.../<timestamp>/`:

```
.artifacts/fill_ingest_staging/<YYYYMMDDTHHMMSS>/    # staging smoke
.artifacts/fill_alert_fire_drill/<YYYYMMDDTHHMMSS>/  # fill alert fire drill
.artifacts/risk_fire_drill/<YYYYMMDDTHHMMSS>/        # kill-switch + drawdown drill
.artifacts/budget_fire_drill/<YYYYMMDDTHHMMSS>/      # budget limits drill
```

The `.artifacts/` directory is **gitignored**. Do not commit evidence files.

### What to paste into PR or incident notes

1. **`summary.txt`** -- exact metric lines, cursor tuples, parsed values. Copy/paste as-is.
2. **`sha256sums.txt`** -- full 64-char hashes of all artifact files (integrity proof).
3. **Terminal output** -- the `=== Results: N passed, 0 failed ===` line.

### Cleanup

Old evidence runs are not auto-deleted. Clean up manually:

```bash
rm -rf .artifacts/fill_ingest_staging/
rm -rf .artifacts/fill_alert_fire_drill/
```

---

## What good looks like

### Local smoke (`smoke_fill_ingest.sh`)

```
=== Fill Ingest Smoke Test ===
  PASS: enabled gauge is 0 when FILL_INGEST_ENABLED=0
  PASS: enabled gauge is 1 when FILL_INGEST_ENABLED=1
  PASS: health metrics appear in /metrics
  PASS: no forbidden labels
=== Results: N passed, 0 failed ===
```

### Staging smoke -- Gate A (`smoke_fill_ingest_staging.sh`)

```
--- Gate A: OFF (FakePort, no API keys) ---
  PASS: enabled=0
  PASS: no forbidden labels
  PASS: fill_line_count >= 10
  artifact: gate_a_metrics.txt  sha256=<64-char-hex>  bytes=3421
```

### Fire drill (`fire_drill_fill_alerts.sh`)

```
--- Drill A: Non-monotonic rejection ---
  PASS: cursor tuple unchanged (9999 1700000000000 ... == 9999 1700000000000 ...)
  PASS: rejected_non_monotonic counter in metrics
  PASS: FILL_CURSOR_REJECTED_NON_MONOTONIC in log output

--- Drill B: Cursor stuck inputs ---
  PASS: polls_total > 0 (scrape 1: 1)
  PASS: cursor_save error > 0 (scrape 2: 3)
  PASS: cursor_age_seconds grew (0.000 -> 8.9xx, delta > 2.0)

=== Results: 18 passed, 0 failed, 0 skipped ===
```

---

## Incident template (copy/paste)

When filing an incident or PR evidence note, use this template:

```
## Incident / Evidence Note

**Date**: YYYY-MM-DD HH:MM UTC
**Alert**: <alert name, e.g. FillCursorStuck>
**Symptom**: <one sentence>

### Script run

```bash
bash scripts/<script_name>.sh
```

**Result**: N passed, 0 failed
**evidence_dir**: .artifacts/.../<timestamp>/

### Key evidence

<paste summary.txt contents here>

### sha256sums

<paste sha256sums.txt contents here>

### Next steps

- [ ] <diagnostic action 1>
- [ ] <diagnostic action 2>
```

---

## Quick links

| Topic | Runbook |
|-------|---------|
| Fill tracker metrics and alerts | [26_FILL_TRACKER_TRIAGE.md](26_FILL_TRACKER_TRIAGE.md) |
| Kill-switch and drawdown | [04_KILL_SWITCH.md](04_KILL_SWITCH.md) |
| Active remediation and budget limits | [12_ACTIVE_REMEDIATION.md](12_ACTIVE_REMEDIATION.md) |
| Evidence artifact index | [00_EVIDENCE_INDEX.md](00_EVIDENCE_INDEX.md) |
| General alert response | [06_ALERT_RESPONSE.md](06_ALERT_RESPONSE.md) |
| Health checks | [02_HEALTH_TRIAGE.md](02_HEALTH_TRIAGE.md) |
