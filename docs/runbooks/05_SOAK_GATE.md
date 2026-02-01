# Runbook: Soak Gate

## Overview

The soak gate validates that strategy behavior is stable and consistent before releases. It runs fixture-based tests and checks thresholds.

---

## Components

| Component | Path | Purpose |
|-----------|------|---------|
| Soak fixtures | `scripts/run_soak_fixtures.py` | Run strategy over fixtures |
| Soak thresholds | `monitoring/soak_thresholds.yml` | Pass/fail criteria |
| Gate checker | `scripts/check_soak_gate.py` | Validate against thresholds |

---

## Running Soak Tests

### 1. Run Soak Fixtures

```bash
PYTHONPATH=src python scripts/run_soak_fixtures.py \
  --runs 3 \
  --mode baseline \
  --output artifacts/soak_fixtures.json
```

**Parameters:**
- `--runs`: Number of runs per fixture (default: 3)
- `--mode`: `baseline` or `candidate`
- `--output`: Where to save results

**What good looks like:**

```
Running soak fixtures (mode=baseline, runs=3)...
  sample_day: 3/3 runs complete
  sample_day_allowed: 3/3 runs complete
  ...
Results saved to artifacts/soak_fixtures.json
```

### 2. Check Soak Gate

```bash
python scripts/check_soak_gate.py \
  --report artifacts/soak_fixtures.json \
  --thresholds monitoring/soak_thresholds.yml \
  --mode baseline
```

**What good looks like (PASS):**

```
============================================================
SOAK GATE REPORT
============================================================
Mode: baseline
Fixtures: 6/6 passed

Fixture                  Signals  Deterministic  Status
----------------------------------------------------------
sample_day               42       YES            PASS
sample_day_allowed       38       YES            PASS
...

============================================================
FINAL VERDICT: PASS
============================================================
```

**What bad looks like (FAIL):**

```
============================================================
FINAL VERDICT: FAIL
  ! sample_day: non-deterministic (run 1 != run 2)
  ! sample_day_toxic: signals below threshold (got 5, min 10)
============================================================
```

---

## Thresholds Configuration

File: `monitoring/soak_thresholds.yml`

```yaml
baseline:
  min_signals_per_fixture: 10
  require_determinism: true
  max_runtime_seconds: 300

candidate:
  min_signals_per_fixture: 10
  require_determinism: true
  max_runtime_seconds: 300
```

---

## Interpreting Failures

### Non-Deterministic Runs

**Symptom:**
```
! sample_day: non-deterministic (run 1 != run 2)
```

**Cause:** Same fixture produced different outputs across runs.

**Action:**
1. Check for floating-point issues
2. Check for timestamp dependencies
3. Check for random/shuffle operations
4. Run determinism suite: `python scripts/verify_determinism_suite.py -v`

### Signals Below Threshold

**Symptom:**
```
! sample_day_toxic: signals below threshold (got 5, min 10)
```

**Cause:** Strategy produced fewer signals than expected.

**Action:**
1. Verify fixture data is correct
2. Check if strategy logic changed
3. Review threshold appropriateness

### Timeout

**Symptom:**
```
! sample_day: timeout (exceeded 300s)
```

**Cause:** Fixture took too long to process.

**Action:**
1. Check for infinite loops
2. Profile performance
3. Consider increasing timeout threshold

---

## CI Integration

Soak gate runs automatically on:
- Every PR via `soak-gate` workflow
- Merge to main

Workflow file: `.github/workflows/soak-gate.yml`

### Viewing CI Results

1. Go to PR > Checks > soak-gate
2. Expand job output for detailed report
3. Download artifacts for full JSON report

---

## Manual Re-Run

If soak gate failed in CI and you want to reproduce locally:

```bash
# Clean artifacts
rm -rf artifacts/

# Run soak fixtures
PYTHONPATH=src python scripts/run_soak_fixtures.py \
  --runs 3 --mode baseline --output artifacts/soak_fixtures.json

# Check gate
python scripts/check_soak_gate.py \
  --report artifacts/soak_fixtures.json \
  --thresholds monitoring/soak_thresholds.yml \
  --mode baseline
```
