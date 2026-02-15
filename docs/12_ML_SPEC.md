# GRINDER - ML Specification

> Machine learning for parameter calibration and policy discovery

**Status:** M8 milestone — implementation planned
**SSOT:** This document defines ML integration contracts
**See also:** ADR-064 (ML Integration), docs/05_FEATURE_CATALOG.md

---

## Table of Contents

1. [ML Philosophy](#121-ml-philosophy)
2. [M8 I/O Contracts (SSOT)](#122-m8-io-contracts-ssot)
3. [Determinism Invariants](#123-determinism-invariants)
4. [Artifact Versioning Scheme](#124-artifact-versioning-scheme)
5. [Runtime Enablement Model](#125-runtime-enablement-model)
6. [M8 Milestone Plan](#126-m8-milestone-plan)
7. [ML Use Cases](#127-ml-use-cases)
8. [Feature Engineering for ML](#128-feature-engineering-for-ml)
9. [Model Training Pipeline](#129-model-training-pipeline)
10. [Model Registry](#1210-model-registry)
11. [Model Monitoring](#1211-model-monitoring)
12. [Calibration Path](#1212-calibration-path)

---

## 12.1 ML Philosophy

```
┌─────────────────────────────────────────────────────────────────┐
│                      ML IN GRINDER                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ML is NOT for:                                                 │
│  ❌ Predicting price direction                                  │
│  ❌ Timing entries/exits                                        │
│  ❌ Black-box trading decisions                                 │
│                                                                  │
│  ML IS for:                                                     │
│  ✓ Calibrating grid parameters                                  │
│  ✓ Optimizing policy thresholds                                 │
│  ✓ Discovering new policy rules (offline)                       │
│  ✓ Estimating fill probabilities                                │
│  ✓ Predicting toxicity regimes                                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12.2 M8 I/O Contracts (SSOT)

This section defines the canonical input/output contracts for ML integration.
All implementations MUST conform to these contracts.

### 12.2.1 Input: Feature Snapshots

ML models receive two feature snapshot types as input:

#### FeatureSnapshot (L1 + Volatility)

```python
@dataclass(frozen=True)
class FeatureSnapshot:
    """L1 microstructure + volatility features.

    Source: src/grinder/features/types.py
    """
    ts: int                    # Timestamp (ms)
    symbol: str                # Trading symbol

    # L1 microstructure
    mid_price: Decimal         # Mid price (bid + ask) / 2
    spread_bps: int            # Bid-ask spread in integer bps
    imbalance_l1_bps: int      # L1 imbalance [-10000, 10000]
    thin_l1: Decimal           # Min(bid_qty, ask_qty)

    # Volatility
    natr_bps: int              # Normalized ATR in integer bps
    atr: Decimal | None        # Raw ATR (None if warmup)

    # Range/trend
    sum_abs_returns_bps: int   # Sum of absolute returns
    net_return_bps: int        # Net return over horizon
    range_score: int           # Choppiness indicator

    # Metadata
    warmup_bars: int           # Completed bars available
```

#### L2FeatureSnapshot (Order Book Depth)

```python
@dataclass(frozen=True)
class L2FeatureSnapshot:
    """L2 order book features.

    Source: src/grinder/features/l2_types.py
    Field names: SPEC_V2_0.md §B.2 (*_topN_* naming)
    """
    ts_ms: int                 # Timestamp (ms)
    symbol: str                # Trading symbol
    venue: str                 # Exchange venue
    depth: int                 # Levels per side (topN)

    # Depth features
    depth_bid_qty_topN: Decimal
    depth_ask_qty_topN: Decimal
    depth_imbalance_topN_bps: int  # [-10000, 10000]

    # Impact features (VWAP slippage)
    impact_buy_topN_bps: int
    impact_sell_topN_bps: int
    impact_buy_topN_insufficient_depth: int   # 0 or 1
    impact_sell_topN_insufficient_depth: int  # 0 or 1

    # Wall features
    wall_bid_score_topN_x1000: int
    wall_ask_score_topN_x1000: int

    # Config
    qty_ref: Decimal           # Reference qty for impact calc
```

### 12.2.2 Output: MlSignalSnapshot

ML models produce a single output type:

```python
@dataclass(frozen=True)
class MlSignalSnapshot:
    """ML model output signal.

    All probabilities are integers in basis points [0, 10000].
    This ensures deterministic serialization/comparison.

    SSOT: docs/12_ML_SPEC.md §12.2.2
    """
    ts: int                    # Timestamp when computed (ms)
    symbol: str                # Trading symbol
    model_version: str         # Model artifact version (e.g., "v1.0.0")
    model_hash: str            # SHA256 of model artifact (16-char hex)

    # Regime probabilities (sum = 10000 bps = 100%)
    regime_low_prob_bps: int   # P(LOW regime) in bps
    regime_mid_prob_bps: int   # P(MID regime) in bps
    regime_high_prob_bps: int  # P(HIGH regime) in bps

    # Predicted regime (argmax of probabilities)
    predicted_regime: str      # "LOW" | "MID" | "HIGH"
    regime_confidence_bps: int # Max probability in bps

    # Parameter adjustments (multipliers as x1000 integers)
    spacing_multiplier_x1000: int   # 1000 = 1.0x, 1500 = 1.5x

    # Feature importance (top 3, for interpretability)
    top_features: tuple[str, str, str]
    top_feature_weights_x1000: tuple[int, int, int]

    # Metadata
    inference_latency_us: int  # Inference time in microseconds
    features_hash: str         # SHA256 of input features (16-char hex)
```

### 12.2.3 Contract Invariants

**Input contracts:**
- All integer fields use basis points (bps) or x1000 encoding
- All Decimal fields serialize as strings
- `ts`/`ts_ms` are Unix milliseconds (int)
- Missing features → model MUST handle gracefully (return neutral signal)

**Output contracts:**
- `regime_*_prob_bps` MUST sum to exactly 10000
- `predicted_regime` MUST be one of: `"LOW"`, `"MID"`, `"HIGH"`
- `regime_confidence_bps` = max(regime_low_prob_bps, regime_mid_prob_bps, regime_high_prob_bps)
- `spacing_multiplier_x1000` MUST be in range [500, 2000] (0.5x to 2.0x)
- `model_hash` MUST match artifact manifest (see §12.4)
- `features_hash` = SHA256 of JSON-serialized inputs, truncated to 16 hex chars

**JSON serialization:**
```json
{
  "ts": 1700000000000,
  "symbol": "BTCUSDT",
  "model_version": "v1.0.0",
  "model_hash": "a1b2c3d4e5f67890",
  "regime_low_prob_bps": 2000,
  "regime_mid_prob_bps": 5000,
  "regime_high_prob_bps": 3000,
  "predicted_regime": "MID",
  "regime_confidence_bps": 5000,
  "spacing_multiplier_x1000": 1000,
  "top_features": ["natr_bps", "spread_bps", "depth_imbalance_topN_bps"],
  "top_feature_weights_x1000": [350, 280, 220],
  "inference_latency_us": 1500,
  "features_hash": "f0e1d2c3b4a59687"
}
```

---

## 12.3 Determinism Invariants

ML integration MUST preserve replay determinism. This section lists invariants.

### 12.3.1 MUST (Required)

| Invariant | Description |
|-----------|-------------|
| **DET-01** | Same input features → same MlSignalSnapshot output (bitwise identical) |
| **DET-02** | Model weights loaded from artifact file (no random init) |
| **DET-03** | All floating-point outputs converted to integer bps/x1000 via `round()` |
| **DET-04** | Feature ordering matches canonical order in FeatureSnapshot/L2FeatureSnapshot |
| **DET-05** | Model version + hash included in output for traceability |
| **DET-06** | Inference uses `float32` or `float64` (no mixed precision) |
| **DET-07** | NumPy/ONNX random seed fixed at model load time |
| **DET-08** | features_hash computed BEFORE inference, logged with output |

### 12.3.2 MUST NOT (Forbidden)

| Anti-pattern | Why forbidden |
|--------------|---------------|
| **NODET-01** | Random dropout/noise at inference time |
| **NODET-02** | Time-based or clock-based features in model |
| **NODET-03** | External API calls during inference |
| **NODET-04** | Mutable global state affecting inference |
| **NODET-05** | Float comparison without epsilon tolerance |
| **NODET-06** | Thread-local storage for model state |
| **NODET-07** | Dynamic feature selection based on runtime conditions |

### 12.3.3 Verification Protocol

```bash
# Run determinism check: same inputs → same outputs across 2 runs
python -m scripts.verify_ml_determinism \
    --fixture tests/fixtures/sample_day \
    --model var/models/regime_v1/

# Expected output:
# Input features hash:  a1b2c3d4e5f67890
# Run 1 output hash:    f0e1d2c3b4a59687
# Run 2 output hash:    f0e1d2c3b4a59687
# Determinism check:    ✅ PASS
```

---

## 12.4 Artifact Versioning Scheme

ML model artifacts follow a strict versioning scheme for reproducibility.

### 12.4.1 Directory Structure

```
var/models/
└── regime_v1/
    ├── manifest.json       # Metadata + checksums (SSOT)
    ├── model.onnx          # Model weights (ONNX format)
    ├── scaler.joblib       # Feature scaler (optional)
    └── config.json         # Hyperparameters
```

### 12.4.2 manifest.json Schema

```json
{
  "schema_version": 1,
  "model_name": "regime_classifier",
  "model_version": "v1.0.0",
  "created_at": "2024-01-15T10:30:00Z",
  "framework": "lightgbm",
  "export_format": "onnx",

  "files": {
    "model.onnx": {
      "sha256": "a1b2c3d4e5f6789012345678901234567890123456789012345678901234abcd",
      "size_bytes": 102400
    },
    "scaler.joblib": {
      "sha256": "b2c3d4e5f6789012345678901234567890123456789012345678901234abcde",
      "size_bytes": 2048
    },
    "config.json": {
      "sha256": "c3d4e5f6789012345678901234567890123456789012345678901234abcdef",
      "size_bytes": 512
    }
  },

  "model_hash": "a1b2c3d4e5f67890",

  "input_features": [
    "natr_bps",
    "spread_bps",
    "imbalance_l1_bps",
    "range_score",
    "depth_imbalance_topN_bps",
    "impact_buy_topN_bps",
    "impact_sell_topN_bps"
  ],

  "output_classes": ["LOW", "MID", "HIGH"],

  "training_metadata": {
    "train_samples": 50000,
    "val_samples": 10000,
    "test_accuracy_bps": 7250,
    "data_hash": "d4e5f67890123456"
  }
}
```

### 12.4.3 Checksum Rules

| Field | Computation |
|-------|-------------|
| `files[*].sha256` | SHA256 of file contents (64-char hex) |
| `model_hash` | SHA256 of `model.onnx` truncated to 16 hex chars |
| `data_hash` | SHA256 of training data, truncated to 16 hex chars |

### 12.4.4 Version Naming

```
<model_name>_v<major>.<minor>.<patch>

Examples:
- regime_classifier_v1.0.0  # Initial release
- regime_classifier_v1.0.1  # Bugfix (same architecture)
- regime_classifier_v1.1.0  # New features (backward compatible)
- regime_classifier_v2.0.0  # Breaking change (new I/O contract)
```

**Semantic versioning rules:**
- MAJOR: Breaking changes to I/O contract
- MINOR: New features, backward compatible
- PATCH: Bugfixes, retraining with same architecture

### 12.4.5 Artifact Validation

```python
def validate_artifact(artifact_dir: Path) -> bool:
    """Validate model artifact integrity.

    Returns True if all checksums match manifest.
    """
    manifest = json.load((artifact_dir / "manifest.json").open())

    for filename, meta in manifest["files"].items():
        file_path = artifact_dir / filename
        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_hash != meta["sha256"]:
            raise ArtifactCorruptedError(f"{filename}: hash mismatch")

    return True
```

---

## 12.5 Runtime Enablement Model

ML integration is **disabled by default** for safe rollout.

### 12.5.1 Configuration

```yaml
# config/paper.yaml or config/live.yaml
ml:
  enabled: false              # MUST be explicitly enabled
  model_dir: "var/models/regime_v1"

  # Safety limits
  max_inference_latency_ms: 10
  fallback_on_error: "neutral"  # "neutral" | "previous" | "error"

  # Feature toggles
  use_l2_features: true
  use_l1_features: true
```

### 12.5.2 Enablement Hierarchy

```
┌────────────────────────────────────────────────────────────────┐
│                  ML ENABLEMENT HIERARCHY                        │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Level 1: Global gate                                          │
│  └── ml.enabled = false (default)                              │
│      └── If false: skip ML entirely, use rule-based policy     │
│                                                                 │
│  Level 2: Artifact gate                                        │
│  └── Model artifact must exist and validate                    │
│      └── If missing/invalid: log warning, use rule-based       │
│                                                                 │
│  Level 3: Runtime gate                                         │
│  └── Inference must complete within max_latency_ms             │
│      └── If timeout: log warning, use fallback                 │
│                                                                 │
│  Level 4: Output gate                                          │
│  └── MlSignalSnapshot must pass contract validation            │
│      └── If invalid: log error, use fallback                   │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

### 12.5.3 Fallback Behavior

| `fallback_on_error` | Behavior |
|---------------------|----------|
| `"neutral"` | Return neutral signal: `predicted_regime="MID"`, `spacing_multiplier_x1000=1000` |
| `"previous"` | Return last valid signal for this symbol |
| `"error"` | Raise exception, halt processing |

### 12.5.4 Safe Rollout Path

```
Phase 1: Shadow mode (ml.enabled=true, ml.shadow_only=true)
├── ML runs in parallel with rule-based
├── ML output logged but NOT used for decisions
└── Compare ML vs rule-based performance

Phase 2: Canary (ml.enabled=true, ml.canary_pct=5)
├── 5% of symbols use ML decisions
├── Monitor for degradation
└── Auto-rollback if metrics degrade

Phase 3: Full rollout (ml.enabled=true)
├── All symbols use ML decisions
├── Continuous monitoring
└── Manual rollback available
```

---

## 12.6 M8 Milestone Plan

M8 (ML Integration) is divided into three sub-milestones.

### M8-00: Specification (docs-only)

**Scope:** This document
**Deliverables:**
- [x] I/O contracts (FeatureSnapshot → MlSignalSnapshot)
- [x] Determinism invariants (MUST/MUST NOT)
- [x] Artifact versioning scheme (manifest.json + sha256)
- [x] Runtime enablement model (ml_enabled=False default)
- [x] M8 milestone plan

**Acceptance criteria:**
- All sections present in docs/12_ML_SPEC.md
- ADR-064 created in docs/DECISIONS.md
- No code changes

### M8-01: Stub Implementation ✅

**Scope:** Code scaffold with tests, no real model
**Deliverables:**
- [x] `MlSignalSnapshot` dataclass in `src/grinder/ml/__init__.py` (PR #140)
- [x] Time-indexed signal selection `_get_ml_signal(symbol, ts_ms)` with bisect O(log n) (PR #141)
- [x] Integration point in PaperEngine (`ml_enabled` flag)
- [x] 26 unit tests for contracts (14 contract + 12 selection)
- [x] Digest-locked fixtures: `sample_day_ml_multisignal_basic`, `sample_day_ml_multisignal_no_prior` (PR #142)

**Acceptance criteria:**
- [x] `ml_enabled=False` → no ML code path executed
- [x] `ml_enabled=True` + no signal.json → baseline digest (safe-by-default)
- [x] SSOT selection rule: max(signal.ts_ms) where signal.ts_ms <= snapshot.ts_ms
- [x] All tests pass, digest unchanged when disabled

### M8-02: ONNX Integration

**Scope:** Real model inference via ONNX Runtime

#### M8-02a: Artifact Plumbing (no inference) ✅

**Deliverables:**
- [x] `OnnxArtifactManifest` and `OnnxArtifact` types in `src/grinder/ml/onnx/`
- [x] Artifact loader with SHA256 validation
- [x] Config fields: `ml_shadow_mode`, `ml_infer_enabled`, `onnx_artifact_dir`
- [x] `verify_onnx_artifact.py` script
- [x] 19 unit tests for artifact validation

**ONNX Artifact v1 Format:**
```json
{
  "schema_version": "v1",
  "model_file": "model.onnx",
  "sha256": {
    "model.onnx": "a1b2c3..."
  },
  "created_at": "2026-02-14T00:00:00Z",
  "notes": "optional"
}
```

**Validation rules:**
- `schema_version` must be `"v1"`
- `model_file` must exist in `sha256` map
- All paths must be relative (no `..`, no absolute)
- SHA256 must match actual file content

**Safe-by-default:**
- `ml_shadow_mode=False` (default)
- `ml_infer_enabled=False` (default)
- `onnx_artifact_dir=None` (default)
- Existing digests unchanged when all flags are off

#### M8-02b: Shadow Mode ✅

**Deliverables:**
- [x] `OnnxMlModel` with `load_from_dir()` and `predict()` methods
- [x] `vectorize()` for feature vectorization with `FEATURE_ORDER` SSOT
- [x] Model loading with soft-fail (returns None on error)
- [x] Shadow logging (predicted regime, latency metrics)
- [x] Config validation guards (ort check, shadow+infer combo, artifact_dir)
- [x] 19 unit tests (9 model tests + 10 shadow mode tests)
- [x] Tiny test ONNX artifact (`tiny_regime/`)

**ONNX Runtime Integration:**
- `onnxruntime>=1.17,<2.0` added to `[ml]` extras
- `ONNX_AVAILABLE` constant for optional import
- CPU-only execution for determinism (single-threaded)

**Feature Vectorization:**
```python
FEATURE_ORDER = (
    "price_mid", "price_bid", "price_ask", "spread_bps",
    "volume_24h", "volume_1h",
    "volatility_1h_bps", "volatility_24h_bps",
    "position_size", "position_notional", "position_pnl_bps",
    "grid_levels_active", "grid_utilization_pct",
    "trend_strength", "momentum_1h",
)
```

**Config Guards:**
1. `ml_infer_enabled=True && !ONNX_AVAILABLE` → ConfigError
2. `ml_infer_enabled=True && !ml_shadow_mode` → ConfigError (real inference not yet supported)
3. `ml_shadow_mode=True && !ml_infer_enabled` → ConfigError
4. `ml_shadow_mode=True && !onnx_artifact_dir` → ConfigError

#### M8-02c: Active Inference Mode

**Reference:** ADR-065 (Shadow → Active Inference Transition)

**Sub-PRs:**
- M8-02c-0: ADR-065 (docs only) — defines state machine, guards, observability
- M8-02c-1: Config guards + tests (no inference logic)
- M8-02c-2: Active inference implementation

**Key Safety Requirements (from ADR-065):**
- Two-key activation: `ml_infer_enabled` + `ml_active_enabled`
- Explicit ack: `ml_active_ack="I_UNDERSTAND_THIS_AFFECTS_TRADING"`
- Kill-switch: `ML_KILL_SWITCH=1` env var (per-snapshot, instant OFF)
- Fail-closed: ACTIVE errors do NOT mutate policy

**Acceptance criteria:**
- Determinism verified across 2 runs
- Inference latency < 10ms on sample fixture
- 15 guard tests per ADR-065 Test Requirements
- Kill-switch activates immediately (per-snapshot)

#### M8-02c-3: Structured Logs & Observability

**Status:** ✅ Implemented (PR #149)

**Log Events (SSOT):**

| Event | Level | When | Key Fields |
|-------|-------|------|------------|
| `ML_MODE_INIT` | info | Model loaded | `mode`, `artifact_dir` |
| `ML_KILL_SWITCH_ON` | warning | Kill-switch active | `ts`, `symbol`, `reason` |
| `ML_ACTIVE_ON` | info | Inference succeeded | `ts`, `symbol` |
| `ML_ACTIVE_BLOCKED` | warning | ACTIVE blocked | `ts`, `symbol`, `reason`, `latency_ms` (optional) |
| `ML_INFER_OK` | info | Inference success | `ts`, `symbol`, `regime`, `probs_bps`, `spacing_x1000`, `latency_ms`, `artifact_dir` |
| `ML_INFER_ERROR` | error | Inference exception | `ts`, `symbol`, `error`, `latency_ms`, `artifact_dir` |

**Prometheus Metrics:**

| Metric | Type | Description |
|--------|------|-------------|
| `grinder_ml_active_on` | gauge | 1 if ACTIVE inference succeeded this tick, 0 otherwise |
| `grinder_ml_block_total{reason="..."}` | counter | Count of ACTIVE blocks by reason code |
| `grinder_ml_inference_total` | counter | Total successful inferences |
| `grinder_ml_inference_errors_total` | counter | Total inference errors |

**Reason Codes (MlBlockReason enum, priority order):**

1. `KILL_SWITCH_ENV` - `ML_KILL_SWITCH=1` env var active
2. `KILL_SWITCH_CONFIG` - `ml_kill_switch=True` config
3. `INFER_DISABLED` - `ml_infer_enabled=False`
4. `ACTIVE_DISABLED` - `ml_active_enabled=False`
5. `BAD_ACK` - `ml_active_ack` != expected string
6. `ONNX_UNAVAILABLE` - `onnxruntime` not installed
7. `ARTIFACT_DIR_MISSING` - `onnx_artifact_dir` not found
8. `MANIFEST_INVALID` - `manifest.json` invalid or missing
9. `MODEL_NOT_LOADED` - ONNX model is `None`
10. `ENV_NOT_ALLOWED` - `GRINDER_ENV` not in allowlist

**Source files:**
- `src/grinder/ml/metrics.py` - SSOT for reason codes, gauge state, Prometheus export
- `src/grinder/paper/engine.py` - Log emission points
- `tests/unit/test_ml_log_events.py` - caplog tests for log events

### M8-03: Training Pipeline

**Scope:** Reproducible training pipeline for ONNX model artifacts.

#### M8-03b-1: Training/Export Pipeline MVP

**Status:** ✅ Done (PR #152)

**Deliverables:**
- [x] `scripts/train_regime_model.py` CLI for training and ONNX export
- [x] Deterministic data generation with seed control
- [x] RandomForest classifier → ONNX conversion via skl2onnx
- [x] Golden test artifact (`tests/testdata/onnx_artifacts/golden_regime/`)
- [x] 23 unit tests for training pipeline
- [x] 5 integration tests for train→artifact→inference roundtrip

**CLI Usage:**
```bash
# Basic training with defaults
python -m scripts.train_regime_model \
    --out-dir /tmp/regime_v1 \
    --dataset-id production_data

# Custom parameters
python -m scripts.train_regime_model \
    --out-dir /tmp/regime_v2 \
    --dataset-id production_data \
    --seed 42 \
    --n-samples 1000 \
    --notes "Initial production model"
```

**Output Artifact:**
```
<out-dir>/
├── model.onnx          # ONNX model file
├── manifest.json       # Artifact manifest with SHA256
└── train_report.json   # Training metrics and metadata
```

**train_report.json Schema:**
```json
{
  "dataset_id": "production_data",
  "n_samples": 1000,
  "seed": 42,
  "n_features": 15,
  "train_accuracy": 0.95,
  "regime_distribution": {"LOW": 50, "MID": 400, "HIGH": 550},
  "created_at": "2026-02-15T00:00:00Z",
  "model_sha256": "abc123...",
  "onnx_opset_version": 15,
  "sklearn_version": "1.8.0",
  "skl2onnx_version": "1.20.0"
}
```

**Determinism Guarantees:**
- Same `--seed` + `--dataset-id` + `--n-samples` → identical model SHA256
- Achieved by:
  1. Deterministic synthetic data generation (numpy rng with combined seed)
  2. sklearn RandomForest with `random_state=seed`, `n_jobs=1`
  3. Fixed ONNX graph name (skl2onnx defaults to random UUID)

**Model Contract:**
- Input: `"input"` tensor shape `(batch, 15)` matching `FEATURE_ORDER`
- Output: `"regime_probs"` tensor shape `(batch, 3)` for [LOW, MID, HIGH]
- No spacing_multiplier in MVP (defaults to 1.0 in OnnxMlModel)

**Dependencies:**
```toml
# pyproject.toml [project.optional-dependencies]
ml = [
    "scikit-learn>=1.4,<2.0",
    "onnx>=1.15,<2.0",
    "onnxruntime>=1.17,<2.0",
    "skl2onnx>=1.16,<2.0",  # M8-03b: sklearn to ONNX conversion
]
```

**Source files:**
- `scripts/train_regime_model.py` - Training CLI
- `tests/unit/test_train_regime_model.py` - Unit tests
- `tests/integration/test_train_to_artifact_roundtrip.py` - Integration tests
- `tests/testdata/onnx_artifacts/golden_regime/` - Golden test artifact

#### M8-03b-2: Runtime Integration & Determinism

**Status:** ✅ Done (PR #153)

**Scope:** Validate that artifacts from M8-03b-1 integrate correctly with `OnnxMlModel`
runtime and produce bit-for-bit identical predictions for fixed inputs.

**Deliverables:**
- [x] Golden artifact runtime tests (load twice → predict → compare)
- [x] `test_vectorize_order_matches_feature_order_exactly` - SSOT contract verification
- [x] Full FEATURE_ORDER fixture (all 15 features populated)
- [x] Multiple prediction stability test

**Determinism Guarantees:**

Two types of determinism are validated:

1. **Training determinism** (M8-03b-1):
   - Same `--seed` + `--dataset-id` + `--n-samples` → identical model SHA256
   - Verified by comparing model hashes from two independent training runs

2. **Runtime determinism** (M8-03b-2):
   - Same model file + same input features → identical output
   - Verified by loading model twice, predicting, and comparing `regime_probs_bps`
   - No float comparison (all outputs are quantized to bps/x1000 integers)

**Test Matrix:**

| Test | What it validates |
|------|-------------------|
| `test_golden_load_twice_predict_identical` | Runtime determinism across model instances |
| `test_golden_multiple_predictions_stable` | Prediction stability within single instance |
| `test_golden_predict_with_full_feature_vector` | All 15 FEATURE_ORDER features work |
| `test_vectorize_order_matches_feature_order_exactly` | SSOT vectorize contract |
| `test_vectorize_preserves_feature_order_tuple` | FEATURE_ORDER is immutable (15 elements) |

**Source files:**
- `tests/unit/test_onnx_model.py` - Runtime determinism tests
- `src/grinder/ml/onnx/features.py` - SSOT FEATURE_ORDER and vectorize()

---

## 12.7 ML Use Cases

### Use Case 1: Parameter Calibration

**Goal**: Find optimal grid parameters (spacing, levels, sizes) for current market regime.

```python
@dataclass
class CalibrationTarget:
    """What we're optimizing for."""
    metric: str  # "sharpe", "rt_expectancy", "fill_rate"
    constraints: dict[str, tuple[float, float]]  # {"max_dd": (0, 0.05)}

class ParameterCalibrator:
    """Calibrate grid parameters using ML."""

    def __init__(self, model: BaseEstimator):
        self.model = model
        self.feature_names: list[str] = []

    def fit(self, historical_data: pd.DataFrame) -> None:
        """
        Fit calibration model.

        Features: market regime features
        Target: optimal parameters from walk-forward
        """
        X = historical_data[self.feature_names]
        y = historical_data["optimal_spacing"]  # From walk-forward

        self.model.fit(X, y)

    def predict_parameters(self, features: dict) -> dict[str, float]:
        """Predict optimal parameters for current conditions."""
        X = pd.DataFrame([features])[self.feature_names]
        predictions = self.model.predict(X)

        return {
            "spacing_bps": float(predictions[0]),
            # ... other parameters
        }
```

### Use Case 2: Toxicity Prediction

**Goal**: Predict toxicity regime transitions before they happen.

```python
class ToxicityPredictor:
    """Predict toxicity regime changes."""

    def __init__(self):
        self.model = LGBMClassifier(
            n_estimators=100,
            max_depth=5,
            learning_rate=0.1,
        )
        self.classes = ["LOW", "MID", "HIGH"]

    def fit(self, X: pd.DataFrame, y: pd.Series) -> None:
        """Train on historical toxicity transitions."""
        self.model.fit(X, y)

    def predict_proba(self, features: dict) -> dict[str, float]:
        """Predict probability of each toxicity regime."""
        X = pd.DataFrame([features])
        probs = self.model.predict_proba(X)[0]
        return dict(zip(self.classes, probs))

    def predict_transition(self, current_regime: str,
                           features: dict) -> tuple[str, float]:
        """Predict most likely next regime and confidence."""
        probs = self.predict_proba(features)
        next_regime = max(probs, key=probs.get)
        confidence = probs[next_regime]

        return next_regime, confidence
```

### Use Case 3: Fill Probability Model

**Goal**: Estimate probability of limit order filling within time horizon.

```python
class FillProbabilityModel:
    """ML model for fill probability estimation."""

    def __init__(self):
        self.model = LGBMRegressor(
            n_estimators=50,
            max_depth=4,
        )

    def fit(self, orders: pd.DataFrame) -> None:
        """
        Train on historical order outcomes.

        Features:
        - distance_from_mid_bps
        - spread_bps
        - ofi_zscore
        - depth_imbalance
        - volatility

        Target: did_fill (0/1) or time_to_fill
        """
        features = [
            "distance_bps", "spread_bps", "ofi_zscore",
            "depth_imbalance", "natr_14_5m"
        ]
        X = orders[features]
        y = orders["filled"]

        self.model.fit(X, y)

    def predict(self, order_price: float, mid: float,
                features: dict) -> float:
        """Predict fill probability."""
        distance_bps = abs(order_price - mid) / mid * 10000

        X = pd.DataFrame([{
            "distance_bps": distance_bps,
            "spread_bps": features["spread_bps"],
            "ofi_zscore": features.get("ofi_zscore", 0),
            "depth_imbalance": features.get("depth_imbalance", 0),
            "natr_14_5m": features["natr_14_5m"],
        }])

        return float(np.clip(self.model.predict(X)[0], 0, 1))
```

### Use Case 4: Policy Discovery (Offline)

**Goal**: Discover new policy rules from historical data.

```python
class PolicyDiscovery:
    """Discover optimal policy rules from data."""

    def __init__(self):
        self.tree = DecisionTreeClassifier(
            max_depth=5,
            min_samples_leaf=100,
        )

    def discover_rules(self, data: pd.DataFrame) -> list[PolicyRule]:
        """
        Discover decision rules for policy selection.

        Features: market conditions
        Target: best performing policy
        """
        X = data[self.feature_names]
        y = data["best_policy"]

        self.tree.fit(X, y)

        # Extract rules
        rules = self._extract_rules(self.tree)

        # Validate rules
        validated_rules = [
            rule for rule in rules
            if self._validate_rule(rule, data)
        ]

        return validated_rules

    def _extract_rules(self, tree: DecisionTreeClassifier) -> list[PolicyRule]:
        """Extract human-readable rules from decision tree."""
        rules = []
        tree_rules = export_text(tree, feature_names=self.feature_names)

        # Parse tree rules into PolicyRule objects
        # ...

        return rules
```

---

## 12.8 Feature Engineering for ML

```python
class MLFeatureEngine:
    """Generate features for ML models."""

    def __init__(self):
        self.scalers: dict[str, StandardScaler] = {}

    def compute_features(self, raw_features: dict,
                         lookback_features: list[dict]) -> dict:
        """
        Compute ML features from raw features.

        Includes:
        - Current values
        - Rolling statistics
        - Rate of change
        - Cross-feature interactions
        """
        features = {}

        # Current values (normalized)
        for key, value in raw_features.items():
            features[f"{key}_current"] = self._normalize(key, value)

        # Rolling statistics
        if len(lookback_features) >= 10:
            for key in raw_features.keys():
                values = [f.get(key, 0) for f in lookback_features[-10:]]
                features[f"{key}_mean_10"] = np.mean(values)
                features[f"{key}_std_10"] = np.std(values)
                features[f"{key}_trend"] = self._calc_trend(values)

        # Rate of change
        if len(lookback_features) >= 2:
            prev = lookback_features[-2]
            for key in raw_features.keys():
                if key in prev and prev[key] != 0:
                    features[f"{key}_roc"] = (raw_features[key] - prev[key]) / prev[key]

        # Interactions
        features["spread_x_vol"] = (
            features.get("spread_bps_current", 0) *
            features.get("natr_14_5m_current", 0)
        )
        features["ofi_x_depth"] = (
            features.get("ofi_zscore_current", 0) *
            features.get("depth_imbalance_current", 0)
        )

        return features
```

---

## 12.9 Model Training Pipeline

```python
class MLTrainingPipeline:
    """End-to-end ML training pipeline."""

    def __init__(self, config: MLConfig):
        self.config = config

    def train(self, data_path: Path) -> TrainedModel:
        """Full training pipeline."""

        # 1. Load data
        data = self._load_data(data_path)

        # 2. Feature engineering
        features = self._engineer_features(data)

        # 3. Train/val/test split (temporal)
        train, val, test = self._temporal_split(features)

        # 4. Train model with hyperparameter tuning
        model = self._train_with_tuning(train, val)

        # 5. Evaluate on test set
        metrics = self._evaluate(model, test)

        # 6. SHAP analysis for interpretability
        shap_values = self._compute_shap(model, test)

        # 7. Generate report
        report = self._generate_report(model, metrics, shap_values)

        return TrainedModel(
            model=model,
            metrics=metrics,
            shap_values=shap_values,
            report=report,
            trained_at=datetime.now(),
            data_hash=self._hash_data(data),
        )

    def _train_with_tuning(self, train: pd.DataFrame,
                           val: pd.DataFrame) -> BaseEstimator:
        """Train with Optuna hyperparameter tuning."""

        def objective(trial):
            params = {
                "n_estimators": trial.suggest_int("n_estimators", 50, 200),
                "max_depth": trial.suggest_int("max_depth", 3, 8),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3),
                "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
            }

            model = LGBMRegressor(**params)
            model.fit(
                train[self.feature_cols],
                train[self.target_col],
                eval_set=[(val[self.feature_cols], val[self.target_col])],
                callbacks=[early_stopping(50)],
            )

            preds = model.predict(val[self.feature_cols])
            return mean_squared_error(val[self.target_col], preds)

        study = optuna.create_study(direction="minimize")
        study.optimize(objective, n_trials=self.config.n_trials)

        # Train final model with best params
        best_model = LGBMRegressor(**study.best_params)
        best_model.fit(train[self.feature_cols], train[self.target_col])

        return best_model
```

---

## 12.10 Model Registry

```python
class ModelRegistry:
    """Registry for trained ML models."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.metadata_file = storage_path / "registry.json"
        self.metadata: dict = self._load_metadata()

    def register(self, model: TrainedModel, name: str,
                 version: str) -> str:
        """Register a trained model."""
        model_id = f"{name}_{version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Save model
        model_path = self.storage_path / model_id
        model_path.mkdir(parents=True)
        joblib.dump(model.model, model_path / "model.joblib")

        # Save metadata
        self.metadata[model_id] = {
            "name": name,
            "version": version,
            "trained_at": model.trained_at.isoformat(),
            "metrics": model.metrics,
            "data_hash": model.data_hash,
            "status": "staged",  # staged -> production -> deprecated
        }
        self._save_metadata()

        return model_id

    def load(self, model_id: str) -> BaseEstimator:
        """Load a registered model."""
        model_path = self.storage_path / model_id / "model.joblib"
        return joblib.load(model_path)

    def promote_to_production(self, model_id: str) -> None:
        """Promote model to production."""
        # Demote current production model
        for mid, meta in self.metadata.items():
            if meta.get("status") == "production":
                meta["status"] = "deprecated"

        self.metadata[model_id]["status"] = "production"
        self._save_metadata()

    def get_production_model(self, name: str) -> BaseEstimator | None:
        """Get current production model."""
        for model_id, meta in self.metadata.items():
            if meta["name"] == name and meta["status"] == "production":
                return self.load(model_id)
        return None
```

---

## 12.11 Model Monitoring

```python
class ModelMonitor:
    """Monitor model performance in production."""

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.predictions: list[tuple[dict, float, float]] = []  # (features, pred, actual)

    def record_prediction(self, features: dict,
                          prediction: float,
                          actual: float | None = None) -> None:
        """Record a prediction for monitoring."""
        self.predictions.append((features, prediction, actual))

    def check_drift(self) -> DriftReport:
        """Check for feature drift and prediction drift."""
        if len(self.predictions) < 100:
            return DriftReport(has_drift=False, details={})

        # Feature drift (compare recent vs historical)
        recent_features = [p[0] for p in self.predictions[-100:]]
        historical_features = [p[0] for p in self.predictions[:-100]]

        drift_scores = {}
        for feature in recent_features[0].keys():
            recent_vals = [f[feature] for f in recent_features]
            hist_vals = [f[feature] for f in historical_features]

            # KS test for drift
            ks_stat, p_value = ks_2samp(recent_vals, hist_vals)
            if p_value < 0.05:
                drift_scores[feature] = ks_stat

        # Prediction accuracy drift
        recent_with_actual = [p for p in self.predictions[-100:] if p[2] is not None]
        if recent_with_actual:
            recent_errors = [abs(p[1] - p[2]) for p in recent_with_actual]
            hist_errors = [abs(p[1] - p[2]) for p in self.predictions[:-100] if p[2] is not None]
            if hist_errors:
                accuracy_drift = np.mean(recent_errors) / np.mean(hist_errors) - 1

        return DriftReport(
            has_drift=len(drift_scores) > 0 or accuracy_drift > 0.2,
            feature_drift=drift_scores,
            accuracy_drift=accuracy_drift,
        )
```

---

## 12.12 Calibration Path

```
┌─────────────────────────────────────────────────────────────────┐
│                    CALIBRATION PATH                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. OFFLINE BACKTEST                                            │
│     └── Walk-forward optimization                               │
│     └── Parameter sweep                                         │
│     └── Generate calibration dataset                            │
│                                                                  │
│  2. SHADOW MODE                                                 │
│     └── Run ML predictions alongside rule-based                 │
│     └── Compare performance (no real trades)                    │
│     └── Collect prediction accuracy data                        │
│                                                                  │
│  3. CANARY DEPLOYMENT                                           │
│     └── Small allocation (1-5% of capital)                      │
│     └── Monitor real performance vs shadow                      │
│     └── A/B test old vs new parameters                          │
│                                                                  │
│  4. PRODUCTION ROLLOUT                                          │
│     └── Gradual increase in allocation                          │
│     └── Continuous monitoring                                   │
│     └── Automated rollback on degradation                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12.13 Model Configuration

```yaml
# config/ml.yaml
ml:
  enabled: true

  calibration:
    model: "lightgbm"
    retrain_interval_days: 7
    min_training_samples: 10000

  toxicity_prediction:
    model: "lightgbm"
    lookback_windows: [10, 30, 60]
    prediction_horizon_s: 60

  fill_probability:
    model: "lightgbm"
    features:
      - "distance_bps"
      - "spread_bps"
      - "ofi_zscore"
      - "depth_imbalance"
      - "natr_14_5m"

  monitoring:
    drift_check_interval_hours: 1
    accuracy_threshold: 0.7
    drift_threshold: 0.2

  registry:
    path: "models/"
    max_versions: 10
```
