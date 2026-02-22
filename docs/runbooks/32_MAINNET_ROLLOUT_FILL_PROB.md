# Runbook 32: Mainnet Rollout Ceremony — Fill Probability Enforcement

End-to-end operator ceremony for enabling fill probability enforcement on mainnet.

**Prerequisites:** Complete Runbook 31 (shadow mode, preflight, eval report, model trained).
**SSOT for env vars:** [Runbook 31 Environment Variables Reference](31_FILL_PROB_ROLLOUT.md#environment-variables-reference).

---

## Phase 0: Preconditions (must-pass before any enforcement)

### 0.1 Artifacts exist

Verify all required paths are set and populated:

```bash
# Model directory
ls "$GRINDER_FILL_MODEL_DIR"/manifest.json "$GRINDER_FILL_MODEL_DIR"/model.json

# Eval report directory
ls "$GRINDER_FILL_PROB_EVAL_DIR"/eval_report.json "$GRINDER_FILL_PROB_EVAL_DIR"/manifest.json

# Artifact directory (for evidence + threshold_resolution files)
echo "GRINDER_ARTIFACT_DIR=$GRINDER_ARTIFACT_DIR"
```

### 0.2 Preflight passes

Run preflight with the planned config (same env vars as production):

```bash
python3 -m scripts.preflight_fill_prob \
    --model "$GRINDER_FILL_MODEL_DIR" \
    --eval "$GRINDER_FILL_PROB_EVAL_DIR" \
    --auto-threshold
```

Expected output includes:
- `configured_bps`, `recommended_bps`, `effective_bps`, `mode`
- All checks PASS

### 0.3 Starting state

| Env Var | Value |
|---------|-------|
| `GRINDER_FILL_MODEL_ENFORCE` | `0` (shadow) |
| `GRINDER_FILL_PROB_AUTO_THRESHOLD` | `0` (recommend-only) |
| `GRINDER_FILL_PROB_ENFORCE_SYMBOLS` | _(unset)_ |
| `GRINDER_FILL_PROB_MIN_BPS` | configured value (e.g. `2500`) |

### 0.4 Go/No-Go

- Preflight PASS: **go**
- Preflight FAIL or `FILL_PROB_THRESHOLD_RESOLUTION_FAILED` on startup: **STOP, triage with Runbook 31 failure modes table**

---

## Phase 1: Shadow Baseline (24-48h)

**Goal:** Confirm model/threshold resolution are stable, evidence artifacts are written, no surprises.

### Config

```bash
GRINDER_FILL_MODEL_ENFORCE=0
GRINDER_FILL_PROB_AUTO_THRESHOLD=0      # recommend-only
GRINDER_FILL_PROB_EVAL_DIR=<path>       # eval report for threshold resolution
GRINDER_FILL_MODEL_DIR=<path>           # model for provenance check
GRINDER_ARTIFACT_DIR=<path>             # evidence + threshold_resolution output
```

### Monitoring checklist

| What | How | Expect |
|------|-----|--------|
| Resolution status | Log: `FILL_PROB_THRESHOLD_RESOLUTION_OK` at startup | Present, `mode=recommend_only` |
| Threshold gauge | `grinder_router_fill_prob_auto_threshold_bps` | >0 (recommended value from eval report) |
| Resolution failures | Log: `FILL_PROB_THRESHOLD_RESOLUTION_FAILED` | Absent |
| Evidence artifacts | `ls $GRINDER_ARTIFACT_DIR/threshold_resolution_*.json` | Created on each restart |
| Enforce gauge | `grinder_router_fill_prob_enforce_enabled` | `0` (shadow) |
| Blocks counter | `grinder_router_fill_prob_blocks_total` | `0` (shadow never blocks) |
| CB trips | `grinder_router_fill_prob_cb_trips_total` | `0` |

### Duration

Minimum 24h. Prefer 48h if market is low-volatility (want to see at least one volatile session).

### Go/No-Go

- Resolution OK is stable across restarts: **go to Phase 2**
- Recommended threshold "jumps" without model/eval change: **STOP, investigate eval report**
- `THRESHOLD_RESOLVE_SKIPPED reason=model_dir_unset`: **STOP, fix env vars**

---

## Phase 2: Canary Enforcement — Symbol Allowlist (minimum blast radius)

**Goal:** Enable enforcement for 1-2 low-value symbols only.

### Config change

```bash
GRINDER_FILL_MODEL_ENFORCE=1
GRINDER_FILL_PROB_ENFORCE_SYMBOLS="SYM1,SYM2"   # start small
GRINDER_FILL_PROB_AUTO_THRESHOLD=0                # keep recommend-only for now
```

Restart the instance.

### Post-restart verification (immediate)

| What | How | Expect |
|------|-----|--------|
| Enforce enabled | `grinder_router_fill_prob_enforce_enabled` | `1` |
| Allowlist active | `grinder_router_fill_prob_enforce_allowlist_enabled` | `1` |
| Resolution OK | Log: `FILL_PROB_THRESHOLD_RESOLUTION_OK` | Present |
| CB trips | `grinder_router_fill_prob_cb_trips_total` | `0` |

### Observation window

Minimum 1-4h. Prefer 24h if volume allows.

| What | How | Expect |
|------|-----|--------|
| Blocks counter | `grinder_router_fill_prob_blocks_total` | >0 (expected for enforced symbols) |
| CB trips | `grinder_router_fill_prob_cb_trips_total` | `0` (any trip = rollback) |
| Non-allowlisted symbols | Should trade normally | No blocks for symbols not in allowlist |

### Go/No-Go

- Blocks are "reasonable" (not 100% of orders), no CB trips: **go to Phase 3**
- CB trip (`FILL_PROB_CIRCUIT_BREAKER_TRIPPED` in logs): **rollback to R1**
- All orders blocked for canary symbol: **rollback to R2, investigate threshold**

---

## Phase 3: Expand Allowlist (gradual)

**Goal:** Widen enforcement coverage without a second deployment process.

### Process

1. Add a batch of symbols to `GRINDER_FILL_PROB_ENFORCE_SYMBOLS`
2. Restart
3. Verify post-restart checks (same as Phase 2)
4. Observe 1-4h per batch (minimum)
5. Repeat until all desired symbols are covered

### Batch sizing guidance

| Risk appetite | Batch size | Observation per batch |
|---------------|------------|----------------------|
| Conservative | 1-2 symbols | 24h |
| Moderate | 3-5 symbols | 4h |
| Aggressive | 10+ symbols | 1h |

### Go/No-Go (per batch)

Same criteria as Phase 2. Any CB trip → rollback to previous batch or R1.

---

## Phase 4: Full Enforcement (remove allowlist)

**Goal:** Enforce fill probability gate on all symbols.

### Config change

```bash
GRINDER_FILL_MODEL_ENFORCE=1
# Unset or empty — all symbols go through gate:
GRINDER_FILL_PROB_ENFORCE_SYMBOLS=
```

Restart.

### Post-restart verification

| What | How | Expect |
|------|-----|--------|
| Enforce enabled | `grinder_router_fill_prob_enforce_enabled` | `1` |
| Allowlist active | `grinder_router_fill_prob_enforce_allowlist_enabled` | `0` |
| Resolution OK | Log: `FILL_PROB_THRESHOLD_RESOLUTION_OK` | Present |
| CB trips | `grinder_router_fill_prob_cb_trips_total` | `0` |

### Observation window

24h minimum. This is the first time all symbols are enforced.

### Go/No-Go

- Stable for 24h, no CB trips, block rate reasonable: **go to Phase 5 (optional)**
- CB trip: **rollback to R2 (re-add allowlist with fewer symbols)**

---

## Phase 5 (Optional): Auto-Apply Threshold

**Goal:** Let the resolver automatically apply the recommended threshold from the eval report.

**Prerequisite:** Full enforcement (Phase 4) stable for 24h+.

### 5.1 Recommend-only was already running

During Phase 1-4, `GRINDER_FILL_PROB_AUTO_THRESHOLD=0` was set. Check that recommended and configured thresholds are aligned:

```bash
# From startup log:
# FILL_PROB_THRESHOLD_RESOLUTION_OK mode=recommend_only recommended_bps=XXXX configured_bps=YYYY effective_bps=YYYY
```

If `recommended_bps` significantly differs from `configured_bps`, decide whether to accept the recommendation before enabling auto-apply.

### 5.2 Enable auto-apply

```bash
GRINDER_FILL_PROB_AUTO_THRESHOLD=1
```

Restart.

### Post-restart verification

| What | How | Expect |
|------|-----|--------|
| Resolution mode | Log: `FILL_PROB_THRESHOLD_RESOLUTION_OK mode=auto_apply` | Present |
| Threshold gauge | `grinder_router_fill_prob_auto_threshold_bps` | = `effective_bps` from log |
| CB trips | `grinder_router_fill_prob_cb_trips_total` | `0` |

### Observation window

24h. The threshold now updates on each restart from the eval report.

### Go/No-Go

- Stable, effective threshold matches expectation: **ceremony complete**
- Unexpected threshold value: **rollback to R3**

---

## Rollback Playbook

### R1: Emergency — Disable All Enforcement

```bash
GRINDER_FILL_MODEL_ENFORCE=0
```

Restart. Everything returns to shadow mode. No orders blocked.

**When:** CB trip, unexpected 100% block rate, any "stop the bleeding" scenario.

### R2: Step Down — Narrow Allowlist

```bash
GRINDER_FILL_MODEL_ENFORCE=1
GRINDER_FILL_PROB_ENFORCE_SYMBOLS="SAFE_SYM1"   # back to last known-good set
```

Restart. Enforcement continues for a subset only.

**When:** Problems after expanding allowlist or removing it. Want to keep partial enforcement.

### R3: Auto-Threshold Rollback

```bash
GRINDER_FILL_PROB_AUTO_THRESHOLD=0   # back to recommend-only
```

Restart. Threshold falls back to manual `GRINDER_FILL_PROB_MIN_BPS`.

**When:** Auto-apply produces unexpected threshold, or eval report changes unexpectedly.

---

## Quick Reference: Log Events

| Event | Source | Meaning |
|-------|--------|---------|
| `FILL_PROB_THRESHOLD_RESOLUTION_OK` | `engine.py` | Threshold resolved successfully (includes mode, recommended_bps, effective_bps) |
| `FILL_PROB_THRESHOLD_RESOLUTION_FAILED` | `engine.py` | Threshold resolution failed (includes reason_code, detail) |
| `THRESHOLD_RESOLVE_SKIPPED reason=model_dir_unset` | `engine.py` | Model dir not configured, resolution skipped entirely |
| `FILL_PROB_CIRCUIT_BREAKER_TRIPPED` | `fill_prob_gate.py` | CB tripped due to high block rate (includes block_count, rate_pct) |

## Quick Reference: Metrics

| Metric | Type | Meaning |
|--------|------|---------|
| `grinder_router_fill_prob_enforce_enabled` | gauge | 1 = enforcement on, 0 = shadow |
| `grinder_router_fill_prob_enforce_allowlist_enabled` | gauge | 1 = symbol allowlist active, 0 = all symbols |
| `grinder_router_fill_prob_blocks_total` | counter | Orders blocked by fill-prob gate |
| `grinder_router_fill_prob_cb_trips_total` | counter | Circuit breaker trips |
| `grinder_router_fill_prob_auto_threshold_bps` | gauge | Resolved threshold from eval (0 = disabled/failed) |
