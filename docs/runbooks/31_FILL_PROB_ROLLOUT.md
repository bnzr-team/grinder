# Runbook: Fill Probability Enforcement Rollout

## Overview

Step-by-step operator guide for enabling fill probability enforcement.
The gate blocks orders with low predicted fill probability (below threshold).

**Related:** ADR-073 in docs/DECISIONS.md.

---

## Prerequisites

Before enabling enforcement, you MUST have:

1. A trained FillModelV0 model (`ml/models/fill_model_v0/`)
2. An evaluation report (`ml/eval/fill_model_v0/`)
3. Shadow mode evidence artifacts (`ml/evidence/fill_prob/`)
4. The evaluation report shows well-calibrated model (max_error < 500 bps)

---

## Step 1: Run Pre-flight Checks

```bash
python3 -m scripts.preflight_fill_prob \
    --model ml/models/fill_model_v0 \
    --eval ml/eval/fill_model_v0 \
    --evidence-dir ml/evidence/fill_prob \
    --threshold-bps 2500
```

**What good looks like:**

```
============================================================
Fill Probability Enforcement Pre-flight Checks
============================================================
  [PASS] Model loads: Loaded: 8 bins, global_prior=6000 bps
  [PASS] Eval report loads: Loaded: 100 rows evaluated
  [PASS] Calibration: Well-calibrated (max_error=200 bps < 500 bps)
  [PASS] Threshold match: Recommended=2500 bps matches configured=2500 bps
  [PASS] Evidence artifacts: Found 42 evidence artifact(s)

All checks passed. Safe to set GRINDER_FILL_MODEL_ENFORCE=1.
```

**Do NOT proceed if any check shows FAIL.**

---

## Step 2: Review Evaluation Report

```bash
cat ml/eval/fill_model_v0/eval_report.json | python3 -m json.tool | head -20
```

Key fields to review:
- `recommended_threshold_bps` — the threshold that maximizes cost-weighted score
- `calibration_well_calibrated` — must be `true`
- `calibration_max_error_bps` — lower is better (< 500 required)
- `n_rows` — evaluation sample size (more = more confidence)

---

## Step 3: Enable Enforcement

Set environment variables and restart:

```bash
# Required: enable enforcement
export GRINDER_FILL_MODEL_ENFORCE=1

# Optional: override threshold (default: 2500 bps)
export GRINDER_FILL_PROB_MIN_BPS=2500

# Circuit breaker defaults (usually no change needed):
# GRINDER_FILL_PROB_CB_WINDOW_SECONDS=300   (5 min window)
# GRINDER_FILL_PROB_CB_MAX_BLOCK_RATE_PCT=50  (50% max block rate)
```

Restart the service:

```bash
docker restart grinder
```

---

## Step 4: Monitor

### Key Metrics

Watch these metrics after enabling enforcement:

```bash
# Block rate (should be low and stable)
curl -s http://localhost:9090/metrics | grep fill_prob_blocks

# Circuit breaker trips (should be 0)
curl -s http://localhost:9090/metrics | grep fill_prob_cb_trips

# Enforcement enabled flag (should be 1)
curl -s http://localhost:9090/metrics | grep fill_prob_enforce
```

### Expected Behavior

- `grinder_router_fill_prob_blocks_total` — increases slowly (some blocks are expected)
- `grinder_router_fill_prob_cb_trips_total` — stays at 0 (trips indicate over-blocking)
- `grinder_router_fill_prob_enforce_enabled` — shows 1

### Warning Signs

- Block rate climbing rapidly → model may be stale or threshold too high
- Circuit breaker trips → block rate exceeded 50% in 5-min window
- No blocks at all → threshold may be too low (not filtering)

---

## Step 5: Rollback

If anything goes wrong, disable enforcement immediately:

```bash
export GRINDER_FILL_MODEL_ENFORCE=0
docker restart grinder
```

Verify rollback:

```bash
curl -s http://localhost:9090/metrics | grep fill_prob_enforce
# Should show: grinder_router_fill_prob_enforce_enabled 0
```

---

## Circuit Breaker Tuning

The circuit breaker automatically bypasses the gate when block rate exceeds the threshold.

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Window | `GRINDER_FILL_PROB_CB_WINDOW_SECONDS` | 300 (5 min) | Rolling window for rate calculation |
| Max rate | `GRINDER_FILL_PROB_CB_MAX_BLOCK_RATE_PCT` | 50 (50%) | Trips when block rate exceeds this |

To effectively disable the circuit breaker, set max rate to 100%:

```bash
export GRINDER_FILL_PROB_CB_MAX_BLOCK_RATE_PCT=100
```

---

## Structured Logs

On circuit breaker trip:
```
FILL_PROB_CIRCUIT_BREAKER_TRIPPED block_count=15 total_count=20 block_rate_pct=75 window_seconds=300
```

---

## Alert Rules

- **FillProbCircuitBreakerTripped** — fires when `grinder_router_fill_prob_cb_trips_total` increases. Severity: warning. Action: check block rate, review model calibration, consider rollback.

---

## Auto-Threshold Ceremony (PR-C9, PR-A1)

Instead of manually setting `GRINDER_FILL_PROB_MIN_BPS`, the engine can read the recommended threshold directly from the eval report. This section is the **operator ceremony** for safely enabling this feature.

**Related:** ADR-074 in docs/DECISIONS.md.

---

### Environment Variables Reference

| Env Var | Type | Default | Description |
|---------|------|---------|-------------|
| `GRINDER_FILL_PROB_EVAL_DIR` | str | _(unset)_ | Path to eval report dir. **Unset = feature disabled, zero eval reads.** |
| `GRINDER_FILL_MODEL_DIR` | str | _(unset)_ | Path to model dir (for provenance check). |
| `GRINDER_FILL_PROB_AUTO_THRESHOLD` | bool | `0` | `0` = recommend-only (log only). `1` = auto-apply (override threshold). |
| `GRINDER_FILL_PROB_MIN_BPS` | int | `2500` | Configured threshold. Used as fallback when auto-threshold fails or is disabled. |
| `GRINDER_ARTIFACT_DIR` | str | _(unset)_ | If set, evidence artifact `threshold_resolution_{ts}.json` written on resolution. |
| `GRINDER_FILL_MODEL_ENFORCE` | bool | `0` | Must be `1` for enforcement. Auto-threshold only affects threshold value, not enforcement on/off. |

**Safe defaults:** with all env vars unset, auto-threshold is fully disabled. No eval reads, no threshold changes.

---

### Phase 0: Preconditions

Before starting the ceremony, verify:

```bash
# 1. Model directory exists and has manifest.json
ls ml/models/fill_model_v0/manifest.json ml/models/fill_model_v0/model.json

# 2. Eval report directory exists (from eval_fill_model_v0.py)
ls ml/eval/fill_model_v0/eval_report.json ml/eval/fill_model_v0/manifest.json

# 3. Evidence artifacts exist (from shadow mode)
ls ml/evidence/fill_prob/*.json | head -5

# 4. GRINDER_ARTIFACT_DIR set (for threshold resolution evidence)
echo $GRINDER_ARTIFACT_DIR
```

Run pre-flight to validate everything:

```bash
python3 -m scripts.preflight_fill_prob \
    --model ml/models/fill_model_v0 \
    --eval ml/eval/fill_model_v0 \
    --evidence-dir ml/evidence/fill_prob \
    --auto-threshold
```

**All checks must show PASS.** The output will include a threshold summary:

```
============================================================
Threshold Summary
============================================================
  configured_threshold_bps : 2500
  recommended_threshold_bps: 3000
  effective_threshold_bps  : 2500 (recommend-only: no override)
  mode                     : recommend_only
============================================================
```

**Do NOT proceed if any check shows FAIL.**

---

### Phase 1: Recommend-Only (minimum 24-48h)

Enable threshold resolution in **recommend-only mode** (log + metric, no threshold override):

```bash
export GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0
export GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0
export GRINDER_FILL_PROB_AUTO_THRESHOLD=0   # recommend-only (default)

docker restart grinder
```

**Verify on startup log:**

```
THRESHOLD_RESOLVED mode=recommend_only recommended_bps=3000 configured_bps=2500 effective_bps=2500 reason=recommend_only
```

Key points to observe over 24-48h:
- `effective_bps` equals `configured_bps` (no override happened)
- `recommended_bps` is stable across restarts
- `grinder_router_fill_prob_auto_threshold_bps` gauge shows the recommended value (not 0)
- Block rate (`grinder_router_fill_prob_blocks_total`) is not spiking
- No circuit breaker trips (`grinder_router_fill_prob_cb_trips_total` = 0)

---

### Phase 2: Go/No-Go Decision

Compare recommended vs configured and decide whether to auto-apply.

**Go criteria (ALL must be true):**

1. Recommend-only mode ran for >= 24h without issues
2. `recommended_bps` is stable (same value on every restart)
3. `recommended_bps` is within reasonable range of `configured_bps` (delta < 1000 bps)
4. Block rate is acceptable (< 20% of orders blocked)
5. Zero circuit breaker trips
6. Evidence artifact `threshold_resolution_{ts}.json` present (if `GRINDER_ARTIFACT_DIR` set)

**No-go triggers (any one blocks):**

- `recommended_bps` differs wildly from `configured_bps` (delta >= 1000 bps) → investigate model/eval before proceeding
- Block rate climbing or CB tripped → model may be stale
- `grinder_router_fill_prob_auto_threshold_bps` shows 0 → resolution failed, check logs for `THRESHOLD_RESOLVE_FAILED`

```bash
# Quick metrics check
curl -s http://localhost:9090/metrics | grep -E "router_fill_prob_(auto_threshold|blocks|cb_trips|enforce)"
```

---

### Phase 3: Auto-Apply

Enable auto-apply (threshold override from eval report):

```bash
export GRINDER_FILL_PROB_AUTO_THRESHOLD=1   # auto-apply

docker restart grinder
```

**Verify on startup log:**

```
THRESHOLD_RESOLVED mode=auto_apply recommended_bps=3000 configured_bps=2500 effective_bps=3000 reason=auto_applied
```

Note: `effective_bps` now equals `recommended_bps` (not `configured_bps`).

**Monitor closely for the first 30 minutes:**

```bash
# Every 5 minutes:
curl -s http://localhost:9090/metrics | grep -E "router_fill_prob_(auto_threshold|blocks|cb_trips|enforce)"
```

Expected:
- `grinder_router_fill_prob_auto_threshold_bps` shows the new threshold
- `grinder_router_fill_prob_enforce_enabled` = 1
- Block rate stable
- `grinder_router_fill_prob_cb_trips_total` = 0

---

### Rollback

#### Quick rollback: disable auto-apply, keep manual threshold

```bash
export GRINDER_FILL_PROB_AUTO_THRESHOLD=0
docker restart grinder
```

Verify: `effective_bps` equals `configured_bps` in startup log.

#### Full rollback: disable threshold resolution entirely

```bash
unset GRINDER_FILL_PROB_EVAL_DIR
export GRINDER_FILL_PROB_AUTO_THRESHOLD=0
docker restart grinder
```

Verify: no `THRESHOLD_RESOLVED` log line. `grinder_router_fill_prob_auto_threshold_bps` = 0.

#### Emergency: disable enforcement

```bash
export GRINDER_FILL_MODEL_ENFORCE=0
docker restart grinder
```

This disables the entire fill-prob gate (not just auto-threshold).

---

### What to Monitor

| What | Metric / Artifact | Healthy | Unhealthy |
|------|-------------------|---------|-----------|
| Auto-threshold resolved | `grinder_router_fill_prob_auto_threshold_bps` | > 0 (shows resolved value) | = 0 (disabled or failed) |
| Block rate | `rate(grinder_router_fill_prob_blocks_total[5m])` | Low and stable | Climbing rapidly |
| Circuit breaker | `grinder_router_fill_prob_cb_trips_total` | = 0 | > 0 (block rate exceeded 50% in window) |
| Enforcement enabled | `grinder_router_fill_prob_enforce_enabled` | = 1 | = 0 (disabled) |
| Evidence artifacts | `{GRINDER_ARTIFACT_DIR}/fill_prob/threshold_resolution_*.json` | Present after each restart | Missing (ARTIFACT_DIR unset or write error) |
| Startup log | `THRESHOLD_RESOLVED mode=... reason=...` | `reason=auto_applied` or `reason=recommend_only` | `reason=resolution_failed` or absent |

**Alert:** `FillProbCircuitBreakerTripped` fires when `grinder_router_fill_prob_cb_trips_total` increases. Action: check block rate, review model calibration, consider rollback.

---

### Interpreting the Auto-Threshold Gauge

| `grinder_router_fill_prob_auto_threshold_bps` | Meaning |
|----------------------------------------------|---------|
| 0 | Feature disabled (eval_dir unset) OR resolution failed |
| > 0 | Resolved threshold from eval report (both recommend-only and auto-apply) |

In **recommend-only** mode, the gauge shows the recommended value but the actual enforcement threshold remains `GRINDER_FILL_PROB_MIN_BPS`. In **auto-apply** mode, the gauge value IS the enforcement threshold.
