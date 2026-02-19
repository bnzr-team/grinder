# Runbook 00: Evidence Index (Launch-07)

One-table reference: what you want to prove, which script to run, and where to find the artifacts.

See also: [Ops Quickstart](00_OPS_QUICKSTART.md) | [Fill Tracker Triage](26_FILL_TRACKER_TRIAGE.md)

---

## Evidence matrix

| Scenario | What you prove | Script | Artifact dir | Key files | Copy/paste proof |
|----------|---------------|--------|-------------|-----------|-----------------|
| Fill wiring (local) | Metric labels + wiring are sane, no forbidden labels | `scripts/smoke_fill_ingest.sh` | *(no persistent artifacts)* | terminal output | `=== Results: N passed, 0 failed ===` |
| Staging enablement | Gates A/B/C pass, real Binance reads work, cursor persists across restart | `scripts/smoke_fill_ingest_staging.sh` | `.artifacts/fill_ingest_staging/<ts>/` | `gate_a_metrics.txt`, `gate_b_metrics.txt`, `gate_c_metrics.txt`, `cursor_after_run1.json`, `cursor_after_run2.json` | evidence block in terminal output |
| Alert inputs (non-monotonic rejection) | `rejected_non_monotonic` counter increments, cursor unchanged, log marker present | `scripts/fire_drill_fill_alerts.sh` | `.artifacts/fill_alert_fire_drill/<ts>/` | `drill_a_metrics.txt`, `drill_a_log.txt`, `cursor_before_drill_a.json`, `cursor_after_drill_a.json` | `summary.txt` |
| Alert inputs (cursor stuck) | `cursor_save error` counter increments, `cursor_age_seconds` grows over time | `scripts/fire_drill_fill_alerts.sh` | `.artifacts/fill_alert_fire_drill/<ts>/` | `drill_b_metrics_1.txt`, `drill_b_metrics_2.txt`, `drill_b_log.txt`, `cursor_drill_b.json` | `summary.txt` |
| Kill-switch + enforcement | Kill-switch trips, gauge=1, INCREASE_RISK blocked, CANCEL allowed, idempotent latch | `scripts/fire_drill_risk_killswitch_drawdown.sh` | `.artifacts/risk_fire_drill/<ts>/` | `drill_a_metrics.txt`, `drill_a_log.txt` | `summary.txt` |
| Drawdown guard | DrawdownGuardV1 blocks INCREASE_RISK in DRAWDOWN, allows REDUCE_RISK/CANCEL, state latched | `scripts/fire_drill_risk_killswitch_drawdown.sh` | `.artifacts/risk_fire_drill/<ts>/` | `drill_b_metrics.txt`, `drill_b_log.txt` | `summary.txt` |

---

## Artifact details

### Local smoke (`smoke_fill_ingest.sh`)

No persistent artifacts. All assertions are inline in terminal output. Run and check exit code.

### Staging smoke (`smoke_fill_ingest_staging.sh`)

```
.artifacts/fill_ingest_staging/<YYYYMMDDTHHMMSS>/
  gate_a_metrics.txt          # Prometheus text (Gate A: OFF, FakePort)
  gate_b_metrics.txt          # Prometheus text (Gate B: ON, real Binance)
  gate_c_metrics.txt          # Prometheus text (Gate C: restart persistence)
  cursor_after_run1.json      # Cursor state after first real run
  cursor_before_restart.json  # Cursor snapshot before restart (monotonicity check)
  cursor_after_run2.json      # Cursor state after restart run
```

Gate B/C artifacts only present when `BINANCE_API_KEY` and `BINANCE_API_SECRET` are set.

### Fire drill (`fire_drill_fill_alerts.sh`)

```
.artifacts/fill_alert_fire_drill/<YYYYMMDDTHHMMSS>/
  cursor_before_drill_a.json  # Cursor before non-monotonic save attempt
  cursor_after_drill_a.json   # Cursor after (should match before)
  cursor_drill_a.json         # Working cursor file used during Drill A
  drill_a_metrics.txt         # Prometheus metrics after Drill A
  drill_a_log.txt             # Captured stderr (FILL_CURSOR_REJECTED_NON_MONOTONIC)
  cursor_drill_b.json         # Cursor used for Drill B
  drill_b_metrics_1.txt       # Scrape 1: after initial successful save
  drill_b_metrics_2.txt       # Scrape 2: after failed saves + time passage
  drill_b_log.txt             # Captured stderr from Drill B
  summary.txt                 # Copy/paste evidence block with exact metric lines
  sha256sums.txt              # Full 64-char sha256 of all artifact files
```

### Risk fire drill (`fire_drill_risk_killswitch_drawdown.sh`)

```
.artifacts/risk_fire_drill/<YYYYMMDDTHHMMSS>/
  drill_a_metrics.txt      # Full Prometheus text after kill-switch trip
  drill_a_log.txt          # Captured stderr (trip, gate, idempotent markers)
  drill_b_metrics.txt      # Full Prometheus text after drawdown trigger
  drill_b_log.txt          # Captured stderr (state transitions, intent decisions)
  summary.txt              # Copy/paste evidence block with exact metric lines
  sha256sums.txt           # Full 64-char sha256 of all artifact files
```

---

## Notes

- `.artifacts/` is gitignored. Do not commit evidence files.
- Each run creates a timestamped subdirectory. Old runs are not auto-deleted.
- `sha256sums.txt` (fire drill) and inline sha256/bytes (staging smoke) provide integrity proof.
- All scripts exit non-zero on any failure.
- **Triage wrapper**: `bash scripts/ops_fill_triage.sh <mode>` runs the right script, surfaces `evidence_dir`, and prints next-step pointers. See [Ops Quickstart](00_OPS_QUICKSTART.md).
