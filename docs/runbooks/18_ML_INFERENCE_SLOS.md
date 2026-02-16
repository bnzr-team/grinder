# Runbook 18: ML Inference Alerts and SLOs

Operational guide for ML inference alerts and service level objectives (M8-02d).

## Overview

This runbook covers:
- ML inference latency alerts
- ML inference error rate alerts
- ACTIVE mode blocking alerts
- Triage procedures for each alert
- Rollback and recovery steps

---

## Alerts Reference

| Alert | Severity | Meaning |
|-------|----------|---------|
| MlInferenceLatencyHigh | warning | p99 latency > 100ms for 5min |
| MlInferenceLatencyCritical | critical | p99.9 latency > 250ms for 3min |
| MlInferenceErrorRateHigh | warning | Error rate > 5% for 5min |
| MlActiveModePersistentlyBlocked | info | ACTIVE mode blocked for 15min |
| MlInferenceStalled | warning | No inferences for 10min while up |

---

## Service Level Objectives (SLOs)

### SLO-1: Inference Latency

**Objective:** ONNX inference completes within acceptable latency.

| Metric | Target | Warning | Critical |
|--------|--------|---------|----------|
| p95 latency | < 50ms | - | - |
| p99 latency | < 100ms | > 100ms | - |
| p99.9 latency | < 250ms | - | > 250ms |

**PromQL (p99):**
```promql
histogram_quantile(0.99,
  sum(rate(grinder_ml_inference_latency_ms_bucket[5m])) by (le, mode)
)
```

**Alerts:** MlInferenceLatencyHigh, MlInferenceLatencyCritical

### SLO-2: Inference Success Rate

**Objective:** ML inference succeeds without errors.

| Metric | Target | Measurement |
|--------|--------|-------------|
| Success rate | > 95% | 1 - (errors / (success + errors)) |
| Error budget | < 5% errors | `rate(grinder_ml_inference_errors_total[5m])` |

**PromQL (error rate):**
```promql
rate(grinder_ml_inference_errors_total[5m])
/
(rate(grinder_ml_inference_total[5m]) + rate(grinder_ml_inference_errors_total[5m]) + 0.001)
```

**Alert:** MlInferenceErrorRateHigh

### SLO-3: ACTIVE Mode Availability

**Objective:** ACTIVE mode is enabled when expected.

| Metric | Target | Measurement |
|--------|--------|-------------|
| ACTIVE uptime | > 99% | Time with `grinder_ml_active_on=1` / Expected uptime |

**Alert:** MlActiveModePersistentlyBlocked

---

## Key Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grinder_ml_inference_latency_ms` | histogram | mode | Inference latency (shadow/active) |
| `grinder_ml_inference_total` | counter | - | Successful inferences |
| `grinder_ml_inference_errors_total` | counter | - | Inference errors |
| `grinder_ml_active_on` | gauge | - | ACTIVE mode enabled (0/1) |
| `grinder_ml_block_total` | counter | reason | ACTIVE mode blocks by reason |

**Histogram buckets (ms):** 1, 2, 5, 10, 25, 50, 100, 250, 500

---

## Alert Triage Procedures

### MlInferenceLatencyHigh

**Severity:** Warning

**Meaning:** ML inference p99 latency exceeds 100ms for 5 minutes.

**Impact:** Slower policy decisions, potential timeouts.

**Triage Steps:**

1. Check current latency percentiles:
   ```promql
   histogram_quantile(0.99, sum(rate(grinder_ml_inference_latency_ms_bucket[5m])) by (le, mode))
   ```

2. Compare shadow vs active mode latency:
   - If both elevated: likely model or system issue
   - If only active: check policy_features complexity

3. Check system resources:
   ```bash
   # CPU usage
   top -p $(pgrep -f grinder)

   # Memory
   free -h
   ```

4. Check ONNX model size:
   ```bash
   ls -lh $ONNX_ARTIFACT_DIR/model.onnx
   ```

5. If persistent:
   - Consider reducing batch size
   - Optimize model (quantization)
   - Scale horizontally

**Rollback:** Kill-switch via `ML_KILL_SWITCH=1` env var.

---

### MlInferenceLatencyCritical

**Severity:** Critical

**Meaning:** ML inference p99.9 latency exceeds 250ms for 3 minutes.

**Impact:** Severe degradation, potential missed trading opportunities.

**Triage Steps:**

1. Immediate: Consider enabling kill-switch:
   ```bash
   export ML_KILL_SWITCH=1
   ```

2. Check if specific symbols/times affected:
   ```promql
   histogram_quantile(0.999, sum(rate(grinder_ml_inference_latency_ms_bucket[5m])) by (le, mode))
   ```

3. Check for resource contention:
   - Other processes consuming CPU
   - Memory pressure causing swapping
   - Disk I/O issues

4. Review recent model changes:
   - New artifact deployed?
   - Feature set changed?

**Rollback:**
- `ML_KILL_SWITCH=1` to disable all ML inference
- Rollback to previous artifact version
- Restart with reduced feature set

---

### MlInferenceErrorRateHigh

**Severity:** Warning

**Meaning:** More than 5% of ML inferences are failing.

**Impact:** Policy decisions fallback to non-ML logic.

**Triage Steps:**

1. Check error logs:
   ```bash
   grep "ML_INFER_ERROR" /var/log/grinder.log | tail -20
   ```

2. Common error causes:
   - `ONNX_UNAVAILABLE`: onnxruntime not installed
   - `MODEL_NOT_LOADED`: artifact path invalid
   - `MANIFEST_INVALID`: corrupted manifest.json
   - Input shape mismatch: feature vector wrong length

3. Verify artifact integrity:
   ```bash
   python -c "from grinder.ml.onnx import load_artifact; load_artifact('$ONNX_ARTIFACT_DIR')"
   ```

4. Check feature extraction:
   - Missing features in policy_features?
   - NaN/Inf values?

**Rollback:** Kill-switch disables inference; errors stop.

---

### MlActiveModePersistentlyBlocked

**Severity:** Info

**Meaning:** ACTIVE mode has been blocked for 15+ minutes.

**Impact:** ML predictions not affecting trading decisions.

**Triage Steps:**

1. Check block reasons:
   ```promql
   sum by (reason) (rate(grinder_ml_block_total[15m]))
   ```

2. Common block reasons and fixes:

   | Reason | Fix |
   |--------|-----|
   | KILL_SWITCH_ENV | Unset `ML_KILL_SWITCH` env var |
   | KILL_SWITCH_CONFIG | Set `ml_kill_switch=False` in config |
   | INFER_DISABLED | Set `ml_infer_enabled=True` |
   | ACTIVE_DISABLED | Set `ml_active_enabled=True` |
   | BAD_ACK | Check `ml_active_ack` matches expected value |
   | ONNX_UNAVAILABLE | Install onnxruntime |
   | ARTIFACT_DIR_MISSING | Check `onnx_artifact_dir` path |
   | MANIFEST_INVALID | Rebuild/re-download artifact |
   | MODEL_NOT_LOADED | Check artifact loading errors |
   | ENV_NOT_ALLOWED | Add `GRINDER_ENV` to allowlist |

3. If intentionally blocked (kill-switch, staged rollout):
   - This is expected, acknowledge alert

**Rollback:** N/A (this is informational)

---

### MlInferenceStalled

**Severity:** Warning

**Meaning:** No ML inferences (success or error) for 10 minutes while GRINDER is up.

**Impact:** ML features not being generated.

**Triage Steps:**

1. Check if ML is enabled:
   ```bash
   env | grep -E "ML_|ONNX_"
   ```

2. Check config:
   - `ml_infer_enabled=True`?
   - `ml_shadow_mode=True` or `ml_active_enabled=True`?

3. Check if symbols have data:
   - L2 snapshots flowing?
   - Policy decisions being made?

4. Check logs for ML-related messages:
   ```bash
   grep -E "ML_|ONNX_" /var/log/grinder.log | tail -50
   ```

5. If expected (no trading, maintenance):
   - Acknowledge alert

**Rollback:** N/A (restart GRINDER if stuck)

---

## Quick Reference

### Enable ML Inference
```bash
# Remove kill-switch
unset ML_KILL_SWITCH

# Or in config
ml_kill_switch: false
ml_infer_enabled: true
ml_active_enabled: true
ml_active_ack: "I_UNDERSTAND_ML_AFFECTS_TRADING"
```

### Disable ML Inference (Emergency)
```bash
export ML_KILL_SWITCH=1
```

### Check Current State
```promql
# ACTIVE mode status
grinder_ml_active_on

# Block counts
sum by (reason) (grinder_ml_block_total)

# Latency percentiles
histogram_quantile(0.99, sum(rate(grinder_ml_inference_latency_ms_bucket[5m])) by (le))

# Error rate
rate(grinder_ml_inference_errors_total[5m]) / (rate(grinder_ml_inference_total[5m]) + 0.001)
```

---

## Related Documentation

- [12_ML_SPEC.md](../12_ML_SPEC.md) - ML architecture and configuration
- [13_OBSERVABILITY.md](../13_OBSERVABILITY.md) - Metrics definitions
- [04_KILL_SWITCH.md](04_KILL_SWITCH.md) - Kill-switch operations
