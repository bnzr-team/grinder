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
| `GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS` | float | _(unset)_ | Eval report freshness gate (**behavioral control**). Unset = disabled. When set, reports older than N hours cause fail-open. Only checked if report contains timestamp (`ts_ms`, `created_at`, `generated_at`). See ADR-074a. |

**Safe defaults:** with all env vars unset, auto-threshold is fully disabled. No eval reads, no threshold changes, no freshness checks.

**Freshness gate:** `GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS` is a behavioral control — it changes which eval reports the resolver accepts. Test in recommend-only mode before production. If the eval report has no timestamp field, freshness is skipped (pass). See ADR-074a.

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

---

## Canary by Instance (PR-C1)

Run two separate grinder instances — **canary** (enforcement ON) and **control** (enforcement OFF) — with disjoint symbol sets. Compare metrics to validate the fill-prob gate before enabling on all symbols.

**Key principle:** no randomization, no percentages. Each symbol is assigned to exactly one instance. Differences in metrics are attributable to the enforcement gate.

**Related:** ADR-073, ADR-074 in docs/DECISIONS.md.

---

### Canary Prerequisites

Before starting the canary, you MUST have completed:

1. All prerequisite steps from [Prerequisites](#prerequisites) (model, eval, evidence, calibration)
2. Pre-flight passes on both model and eval dirs
3. Shadow mode ran for >= 24h with stable `recommended_bps` (Phase 1 of Auto-Threshold Ceremony)
4. Decided which symbols go to canary vs control (disjoint, no overlap)

---

### Instance Topology

| Instance | Role | `--symbols` | `GRINDER_FILL_MODEL_ENFORCE` | `--metrics-port` |
|----------|------|-------------|------------------------------|------------------|
| `grinder_canary` | canary | Canary symbols (e.g., `BTCUSDT`) | `1` | `9090` |
| `grinder_control` | control | Remaining symbols (e.g., `ETHUSDT`) | `0` | `9091` |

Both instances share the **same model, eval, and threshold config**. The only difference is `GRINDER_FILL_MODEL_ENFORCE`.

**Symbol assignment rules:**

- Disjoint sets — no symbol appears in both instances
- Union covers all traded symbols
- Start canary with **1-2 lower-volume symbols** to limit blast radius
- Gradually move symbols from control to canary as confidence grows

---

### Environment Variables: Canary vs Control

```bash
# --- Shared (identical on both instances) ---
export GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0
export GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0
export GRINDER_FILL_PROB_AUTO_THRESHOLD=0          # recommend-only (safest for canary)
export GRINDER_FILL_PROB_MIN_BPS=2500              # same threshold on both
export GRINDER_FILL_PROB_EVAL_MAX_AGE_HOURS=48     # freshness gate (optional)

# --- Canary-specific ---
export GRINDER_FILL_MODEL_ENFORCE=1                # enforcement ON
export GRINDER_ARTIFACT_DIR=ml/artifacts/canary     # separate evidence dir

# --- Control-specific ---
export GRINDER_FILL_MODEL_ENFORCE=0                # enforcement OFF
export GRINDER_ARTIFACT_DIR=ml/artifacts/control    # separate evidence dir
```

**Critical:** `GRINDER_ARTIFACT_DIR` MUST differ between instances to avoid evidence file collisions.

---

### Launch: CLI Invocation

```bash
# Terminal 1: canary (enforcement ON, subset of symbols)
GRINDER_FILL_MODEL_ENFORCE=1 \
GRINDER_ARTIFACT_DIR=ml/artifacts/canary \
GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0 \
GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0 \
GRINDER_FILL_PROB_MIN_BPS=2500 \
  python3 -m scripts.run_live \
    --symbols BTCUSDT \
    --metrics-port 9090

# Terminal 2: control (enforcement OFF, remaining symbols)
GRINDER_FILL_MODEL_ENFORCE=0 \
GRINDER_ARTIFACT_DIR=ml/artifacts/control \
GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0 \
GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0 \
GRINDER_FILL_PROB_MIN_BPS=2500 \
  python3 -m scripts.run_live \
    --symbols ETHUSDT \
    --metrics-port 9091
```

### Launch: Docker Compose (skeleton)

```yaml
services:
  grinder_canary:
    build: .
    container_name: grinder_canary
    ports: ["9090:9090"]
    environment:
      - GRINDER_FILL_MODEL_ENFORCE=1
      - GRINDER_ARTIFACT_DIR=ml/artifacts/canary
      - GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0
      - GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0
      - GRINDER_FILL_PROB_MIN_BPS=2500
    command: ["--symbols", "BTCUSDT", "--metrics-port", "9090"]

  grinder_control:
    build: .
    container_name: grinder_control
    ports: ["9091:9090"]
    environment:
      - GRINDER_FILL_MODEL_ENFORCE=0
      - GRINDER_ARTIFACT_DIR=ml/artifacts/control
      - GRINDER_FILL_MODEL_DIR=ml/models/fill_model_v0
      - GRINDER_FILL_PROB_EVAL_DIR=ml/eval/fill_model_v0
      - GRINDER_FILL_PROB_MIN_BPS=2500
    command: ["--symbols", "ETHUSDT", "--metrics-port", "9090"]
```

Adapt ports, volumes, and Redis config to your deployment. See `docker-compose.ha.yml` for a full HA template.

---

### Canary Monitoring

Compare canary (port 9090) vs control (port 9091) side-by-side:

```bash
# Block rate (canary should show blocks; control should show 0)
echo "=== canary ===" && curl -s http://localhost:9090/metrics | grep -E "fill_prob_(blocks|cb_trips|enforce)"
echo "=== control ===" && curl -s http://localhost:9091/metrics | grep -E "fill_prob_(blocks|cb_trips|enforce)"

# Auto-threshold gauge (should match on both if same eval report)
echo "=== canary ===" && curl -s http://localhost:9090/metrics | grep fill_prob_auto_threshold_bps
echo "=== control ===" && curl -s http://localhost:9091/metrics | grep fill_prob_auto_threshold_bps
```

| What | Canary (enforce=1) | Control (enforce=0) | Concern |
|------|-------------------|--------------------|---------|
| `grinder_router_fill_prob_blocks_total` | > 0 (expected) | ~0 (no enforced blocks; if non-zero, verify enforce flag and logs) | Canary blocks climbing rapidly → threshold too aggressive |
| `grinder_router_fill_prob_cb_trips_total` | = 0 | = 0 | Any CB trip → immediate rollback |
| `grinder_router_fill_prob_enforce_enabled` | = 1 | = 0 | Mismatch → env misconfiguration |
| `grinder_router_fill_prob_auto_threshold_bps` | Same on both | Same on both | Differs → different eval reports loaded |
| Evidence artifacts | `ml/artifacts/canary/fill_prob/` | `ml/artifacts/control/fill_prob/` | Missing → check GRINDER_ARTIFACT_DIR |
| Startup log: `FILL_PROB_THRESHOLD_RESOLUTION_OK` | Present | Present | Missing/FAILED → check eval_dir, model_dir |

---

### Go/No-Go Criteria

**Go (promote canary to all symbols) — ALL must be true:**

1. Canary ran >= 24h without circuit breaker trips
2. Block rate on canary is stable and acceptable (not climbing)
3. No `FILL_PROB_THRESHOLD_RESOLUTION_FAILED` in canary logs
4. `recommended_bps` stable across restarts on both instances
5. Pre-flight passes: `python3 -m scripts.preflight_fill_prob --model ... --eval ... --evidence-dir ... --auto-threshold`
6. Evidence artifacts present in canary artifact dir

**No-go triggers (any one blocks promotion):**

- Circuit breaker tripped on canary
- Block rate > 30% on canary (model may be stale or threshold too high)
- `FILL_PROB_THRESHOLD_RESOLUTION_FAILED` in logs
- `auto_threshold_bps` differs between canary and control (eval report divergence)

---

### Promote: Move Symbols to Canary

Once go criteria are met, gradually move symbols from control to canary:

```bash
# Step 1: add ETHUSDT to canary, remove from control
# Canary: --symbols BTCUSDT,ETHUSDT  (enforcement ON)
# Control: stopped (no symbols left) or --symbols <next batch>

# Step 2: restart both with updated --symbols
# Step 3: monitor 24h, repeat go/no-go
```

When all symbols are on canary and stable, the canary IS production. Retire the control instance.

---

### Rollback Canary

#### Quick rollback: disable enforcement on canary

```bash
# Set enforce=0 on canary, restart
GRINDER_FILL_MODEL_ENFORCE=0  # on canary instance
# Restart canary
```

Canary still runs its symbols but without enforcement. Verify: `grinder_router_fill_prob_enforce_enabled` = 0.

#### Full rollback: merge symbols back to single instance

```bash
# Stop canary and control
# Start single instance with all symbols, enforcement OFF
python3 -m scripts.run_live \
  --symbols BTCUSDT,ETHUSDT \
  --metrics-port 9090
```

#### Emergency: stop canary entirely

```bash
# Stop canary container/process
# Control continues handling its symbols unaffected
# Start replacement instance for canary symbols with enforce=0
```
