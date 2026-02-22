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
