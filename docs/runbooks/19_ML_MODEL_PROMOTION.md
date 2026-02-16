# Runbook 19: ML Model Promotion

Operational guide for ML model lifecycle: staging, promotion, rollback (M8-03c).

## Overview

This runbook covers:
- Model registry structure and conventions
- Promotion flow: staging → shadow → active
- Pre-promotion checklist
- Rollback procedures
- Emergency procedures

---

## Registry Structure

**SSOT:** `ml/registry/models.json`

```
ml/registry/
└── models.json    # Model registry (staging/shadow/active pointers)

artifacts/onnx/
├── regime_v2026_02_15_001/
│   ├── model.onnx
│   ├── manifest.json
│   └── train_report.json
└── regime_v2026_02_16_001/
    └── ...
```

### Stages

| Stage | Purpose | Trading Impact | Who Can Promote |
|-------|---------|----------------|-----------------|
| staging | Pre-flight validation | None | Any developer |
| shadow | Shadow inference (metrics only) | None | Any developer |
| active | Live trading decisions | **Affects orders** | Operator with ACK |

---

## Promotion Flow

```
                    ┌─────────┐
                    │ staging │
                    └────┬────┘
                         │ verify_onnx_artifact PASS
                         │ unit tests PASS
                         ▼
                    ┌─────────┐
                    │ shadow  │◄────────────────────┐
                    └────┬────┘                     │
                         │ 24h soak window          │
                         │ SLO compliance           │
                         │ dashboard review         │ rollback
                         │ ACK required             │
                         ▼                          │
                    ┌─────────┐                     │
                    │ active  │─────────────────────┘
                    └─────────┘
```

---

## Procedures

### 1. Add New Model to Staging

**When:** New model trained and artifact created.

**Steps:**

1. Train model and create artifact:
   ```bash
   python -m scripts.train_regime_model \
       --out-dir artifacts/onnx/regime_v2026_02_16_001 \
       --dataset-id production_data_v4 \
       --seed 42 \
       --notes "New training data Feb 2026"
   ```

2. Verify artifact:
   ```bash
   python -m scripts.verify_onnx_artifact artifacts/onnx/regime_v2026_02_16_001 -v
   ```

3. Update registry (staging):
   ```json
   {
     "staging": {
       "artifact_id": "regime_v2026_02_16_001",
       "artifact_dir": "artifacts/onnx/regime_v2026_02_16_001",
       "model_sha256": "<from train_report.json>",
       "git_sha": "<current commit>",
       "dataset_id": "production_data_v4",
       "created_at_utc": "2026-02-16T08:00:00Z",
       "notes": "New training data Feb 2026"
     }
   }
   ```

4. Create PR with artifact + registry update.

5. CI validates: `verify_onnx_artifact`, unit tests, determinism.

---

### 2. Promote staging → shadow

**When:** Model passes staging validation, ready for shadow testing.

**Pre-requisites:**
- [ ] `verify_onnx_artifact` PASS
- [ ] Unit tests PASS
- [ ] No regressions in golden artifact tests

**Steps:**

1. Update registry:
   ```json
   {
     "shadow": {
       "artifact_id": "regime_v2026_02_16_001",
       ...
     },
     "staging": null
   }
   ```

2. Add history entry:
   ```json
   {
     "timestamp_utc": "2026-02-16T10:00:00Z",
     "action": "promote",
     "from_stage": "staging",
     "to_stage": "shadow",
     "artifact_id": "regime_v2026_02_16_001",
     "by": "developer@example.com",
     "reason": "Staging validation passed"
   }
   ```

3. Create PR.

4. After merge, deploy with `ml_stage: shadow`.

5. Monitor dashboards:
   - [ML Overview Dashboard](../grafana/dashboards/grinder_ml_overview.json)
   - [ML Latency Dashboard](../grafana/dashboards/grinder_ml_latency.json)

---

### 3. Promote shadow → active

**When:** Model passes shadow soak window with SLO compliance.

**Pre-requisites (ALL REQUIRED):**
- [ ] **Soak window:** 24h minimum in shadow mode
- [ ] **Latency SLO:** p99 < 100ms, p99.9 < 250ms (5m rolling)
- [ ] **Error rate:** < 5%
- [ ] **No alerts:** MlInferenceLatencyHigh/Critical not firing
- [ ] **Dashboard review:** Baseline stable, no anomalies
- [ ] **Explicit ACK:** Required in PR

**Steps:**

1. Verify SLO compliance:
   ```promql
   # p99 latency
   histogram_quantile(0.99, sum(rate(grinder_ml_inference_latency_ms_bucket{mode="shadow"}[24h])) by (le))

   # Error rate
   rate(grinder_ml_inference_errors_total[24h]) / (rate(grinder_ml_inference_total[24h]) + 0.001)
   ```

2. Document evidence in PR body:
   ```markdown
   ## Promotion Evidence

   - Shadow soak: 24h (2026-02-15 10:00 to 2026-02-16 10:00)
   - p99 latency: 42ms (SLO: <100ms) ✅
   - p99.9 latency: 78ms (SLO: <250ms) ✅
   - Error rate: 0.3% (SLO: <5%) ✅
   - Alerts: None firing ✅
   - Dashboard: [link to screenshot]
   ```

3. Update registry:
   ```json
   {
     "active": {
       "artifact_id": "regime_v2026_02_16_001",
       "promoted_at_utc": "2026-02-16T10:00:00Z",
       ...
     },
     "shadow": null
   }
   ```

4. Add history entry with reason.

5. Create PR with **ACK in body:**
   ```markdown
   ACK: I_UNDERSTAND_THIS_AFFECTS_TRADING

   I have reviewed the shadow metrics and confirm this model
   is safe to promote to active trading decisions.
   ```

6. After merge, deploy with `ml_stage: active`.

7. Monitor closely for first 1h:
   - Real-time latency
   - Error rate
   - Block reasons

---

### 4. Rollback active → previous

**When:** Active model causing issues, need immediate rollback.

**Severity:** Emergency — no ACK required.

**Steps:**

1. **Immediate:** If critical, enable kill-switch first:
   ```bash
   export ML_KILL_SWITCH=1
   ```

2. Identify previous artifact_id from history:
   ```bash
   cat ml/registry/models.json | jq '.models.regime_classifier.history[-2]'
   ```

3. Update registry to previous artifact:
   ```json
   {
     "active": {
       "artifact_id": "regime_v2026_02_15_001",  # Previous
       ...
     }
   }
   ```

4. Add history entry:
   ```json
   {
     "timestamp_utc": "2026-02-16T14:00:00Z",
     "action": "rollback",
     "from_stage": "active",
     "to_stage": "active",
     "artifact_id": "regime_v2026_02_15_001",
     "by": "operator@example.com",
     "reason": "EMERGENCY: p99 latency spike to 500ms after promotion"
   }
   ```

5. Create PR (expedited review).

6. After merge, deploy.

7. Disable kill-switch if enabled:
   ```bash
   unset ML_KILL_SWITCH
   ```

8. Post-incident: investigate root cause.

---

## Quick Reference

### Check Current Model State

```bash
# View registry
cat ml/registry/models.json | jq '.models.regime_classifier'

# Current active model
cat ml/registry/models.json | jq '.models.regime_classifier.active.artifact_id'

# Current shadow model
cat ml/registry/models.json | jq '.models.regime_classifier.shadow.artifact_id'

# Promotion history
cat ml/registry/models.json | jq '.models.regime_classifier.history[-3:]'
```

### Verify Artifact

```bash
python -m scripts.verify_onnx_artifact <artifact_dir> -v
```

### Check SLO Compliance (PromQL)

```promql
# p99 latency (shadow, last 24h)
histogram_quantile(0.99, sum(rate(grinder_ml_inference_latency_ms_bucket{mode="shadow"}[24h])) by (le))

# p99.9 latency
histogram_quantile(0.999, sum(rate(grinder_ml_inference_latency_ms_bucket{mode="shadow"}[24h])) by (le))

# Error rate
rate(grinder_ml_inference_errors_total[24h]) / (rate(grinder_ml_inference_total[24h]) + 0.001)

# Active mode status
grinder_ml_active_on
```

### Emergency Kill-Switch

```bash
# Disable all ML inference
export ML_KILL_SWITCH=1

# Re-enable
unset ML_KILL_SWITCH
```

---

## Artifact ID Convention

Format: `{model_name}_v{YYYY}_{MM}_{DD}_{seq}`

| Component | Description |
|-----------|-------------|
| model_name | Model type: `regime`, `spacing`, etc. |
| YYYY_MM_DD | Training date |
| seq | Sequence number (001, 002, ...) |

Examples:
- `regime_v2026_02_15_001` — First regime model trained Feb 15, 2026
- `regime_v2026_02_15_002` — Second regime model same day
- `spacing_v2026_02_16_001` — Spacing model

---

## Troubleshooting

### Model not loading after promotion

1. Check config has correct `ml_registry_path` and `ml_stage`
2. Verify artifact_dir path exists
3. Check `verify_onnx_artifact` output
4. Check logs for `ML_REGISTRY` or `ONNX_` errors

### Latency spike after promotion

1. Compare model sizes:
   ```bash
   ls -lh artifacts/onnx/*/model.onnx
   ```
2. Check if new model has more parameters
3. Consider rollback if SLO breached

### Shadow and active showing different results

This is expected — they're different models. Compare:
```promql
histogram_quantile(0.99, sum(rate(grinder_ml_inference_latency_ms_bucket[5m])) by (le, mode))
```

---

## Related Documentation

- [12_ML_SPEC.md](../12_ML_SPEC.md) - ML architecture and M8-03c spec
- [18_ML_INFERENCE_SLOS.md](18_ML_INFERENCE_SLOS.md) - ML inference alerts and SLOs
- [04_KILL_SWITCH.md](04_KILL_SWITCH.md) - Kill-switch operations
