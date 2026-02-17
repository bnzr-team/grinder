# GRINDER - Feature Store Specification

> Training data governance for reproducible ML pipelines

**Status:** M8-04 — Specification (docs-only, no code yet)
**SSOT:** This document defines dataset contracts, storage layout, and lifecycle
**See also:** `docs/12_ML_SPEC.md` (ML integration), `src/grinder/ml/onnx/features.py` (FEATURE_ORDER)

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Definitions](#2-definitions)
3. [Storage Layout](#3-storage-layout)
4. [Dataset Manifest Schema v1](#4-dataset-manifest-schema-v1)
5. [Validation Rules](#5-validation-rules)
6. [Lifecycle and Governance](#6-lifecycle-and-governance)
7. [Determinism and Reproducibility](#7-determinism-and-reproducibility)
8. [CLI Surfaces (Spec Only)](#8-cli-surfaces-spec-only)
9. [Integration Points](#9-integration-points)

---

## 1. Goals and Non-Goals

### Goals

| Goal | Why |
|------|-----|
| **Reproducibility** | Given a `dataset_id`, anyone can rebuild the exact same training data |
| **Audit trail** | Every ACTIVE model links back to a specific, immutable dataset |
| **Integrity** | SHA256 checksums prevent silent data corruption |
| **SSOT alignment** | Feature schema tied to `FEATURE_ORDER` from `src/grinder/ml/onnx/features.py` |
| **Safe promotion** | `promote_ml_model --stage active` already requires `--dataset-id`; this spec defines what that ID points to |

### Non-Goals

| Non-goal | Rationale |
|----------|-----------|
| **Online feature serving** | Grinder's ML is offline calibration, not real-time feature lookup |
| **Streaming ingestion** | Datasets are built from backtest/replay output, not live streams |
| **Multi-terabyte scale** | Current scope: small datasets (<100MB), synthetic or fixture-derived |
| **Versioned feature transforms** | Feature engineering is in code (`FEATURE_ORDER`); data stores raw values |
| **External data lake** | All datasets live in-repo under `ml/datasets/` (Git LFS for large files if needed) |

---

## 2. Definitions

| Term | Definition |
|------|-----------|
| **dataset_id** | Unique identifier for a dataset. Pattern: `[a-z0-9][a-z0-9._-]{2,64}`. Already used in artifact manifest (`dataset_id` field) and registry pointer. |
| **feature_set** | The ordered tuple of feature names. Currently `FEATURE_ORDER` (15 features). Changes to this tuple require a new `feature_order_hash`. |
| **feature_order_hash** | `SHA256(json.dumps(FEATURE_ORDER))` truncated to 16 hex chars. Ties a dataset to a specific feature schema version. |
| **snapshot** | A single row in the dataset: one timestamp + one symbol + feature values + labels. |
| **partition** | Optional grouping of snapshots (e.g., by day or symbol). Not required for MVP. |
| **label** | Target variable(s) for supervised training. For regime classification: `regime` (0=LOW, 1=MID, 2=HIGH) and `spacing_multiplier`. |
| **golden dataset** | A dataset used for golden artifact generation. Must be deterministic (synthetic with fixed seed) and committed to repo. |

---

## 3. Storage Layout

```
ml/datasets/
├── <dataset_id>/
│   ├── manifest.json          # SSOT: metadata, checksums, provenance
│   ├── data.parquet           # Feature matrix + labels (columnar, compressed)
│   ├── splits.json            # Train/val/test split indices (optional)
│   └── README.md              # Human-readable description (optional)
└── ...
```

### Path Rules

| Rule | Enforcement |
|------|-------------|
| All paths relative to repo root | Hard error on absolute paths |
| No `..` path components | Hard error on traversal |
| Directory name = `dataset_id` | Validated at build time |
| `manifest.json` required | Presence check in validation |
| `data.parquet` required | Presence check in validation |

### File Formats

| File | Format | Rationale |
|------|--------|-----------|
| `data.parquet` | Apache Parquet (snappy compression) | Columnar, typed, deterministic with sorted row order |
| `manifest.json` | JSON | Human-readable, Git-diffable |
| `splits.json` | JSON | Simple index arrays, reproducible |
| `README.md` | Markdown | Optional documentation |

---

## 4. Dataset Manifest Schema v1

```json
{
  "schema_version": "v1",
  "dataset_id": "market_data_2025_q4_btcusdt",
  "created_at_utc": "2026-02-17T10:00:00Z",

  "source": "synthetic",
  "source_description": "generate_synthetic_data(n_samples=1000, seed=42, dataset_id='market_data_2025_q4_btcusdt')",

  "time_range": {
    "start_utc": "2025-10-01T00:00:00Z",
    "end_utc": "2025-12-31T23:59:59Z"
  },

  "feature_order": [
    "price_mid", "price_bid", "price_ask", "spread_bps",
    "volume_24h", "volume_1h",
    "volatility_1h_bps", "volatility_24h_bps",
    "position_size", "position_notional", "position_pnl_bps",
    "grid_levels_active", "grid_utilization_pct",
    "trend_strength", "momentum_1h"
  ],
  "feature_order_hash": "a1b2c3d4e5f67890",

  "label_columns": ["regime", "spacing_multiplier"],
  "row_count": 1000,

  "sha256": {
    "data.parquet": "abc123def456...",
    "splits.json": "789012abc345..."
  },

  "determinism": {
    "seed": 42,
    "git_sha": "93e64007df6e7823b09cdea24237bc8138be28aa",
    "build_command": "python -m scripts.build_dataset --dataset-id market_data_2025_q4_btcusdt --source synthetic --seed 42 --n-samples 1000"
  },

  "git_sha": "93e64007df6e7823b09cdea24237bc8138be28aa",
  "actor": "alice@grinder.dev",
  "notes": "Q4 2025 BTC/USDT synthetic data for regime classifier training"
}
```

### Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | YES | Must be `"v1"` |
| `dataset_id` | string | YES | Pattern: `[a-z0-9][a-z0-9._-]{2,64}` |
| `created_at_utc` | string | YES | ISO 8601 with `Z` suffix |
| `source` | string | YES | One of: `synthetic`, `backtest`, `export`, `manual` |
| `source_description` | string | NO | Human-readable description of how data was generated |
| `time_range` | object | NO | `{start_utc, end_utc}` — null for synthetic data |
| `feature_order` | array | YES | List of feature names matching SSOT `FEATURE_ORDER` |
| `feature_order_hash` | string | YES | SHA256 of `json.dumps(feature_order)`, truncated to 16 hex |
| `label_columns` | array | YES | Column names used as training targets |
| `row_count` | int | YES | Number of rows in `data.parquet` |
| `sha256` | object | YES | Map of filename → SHA256 hex digest (64 chars) |
| `determinism` | object | NO | Seed, git_sha, build_command for reproducibility |
| `determinism.seed` | int | NO | Random seed used in generation |
| `determinism.git_sha` | string | NO | 40-char hex git commit at build time |
| `determinism.build_command` | string | NO | Exact command to reproduce |
| `git_sha` | string | NO | 40-char hex; **REQUIRED if dataset is referenced by ACTIVE model** |
| `actor` | string | NO | Who built this dataset (email or username) |
| `notes` | string | NO | Free-form notes |

### Source Types

| Source | Description | `determinism` required? | `time_range` required? |
|--------|-------------|------------------------|----------------------|
| `synthetic` | Generated by code (e.g., `generate_synthetic_data()`) | YES (seed + command) | NO |
| `backtest` | Extracted from backtest replay output | NO | YES |
| `export` | Exported from live/paper engine logs | NO | YES |
| `manual` | Manually curated or third-party | NO | NO |

---

## 5. Validation Rules

### dataset_id

```
Pattern: [a-z0-9][a-z0-9._-]{2,64}
```

Valid: `market_data_2025_q4_btcusdt`, `synthetic_v1`, `golden.regime.v1`
Invalid: `My Dataset`, `../escape`, `/absolute`, `a` (too short), `A_UPPER`

### feature_order_hash

```python
import hashlib, json
expected = hashlib.sha256(json.dumps(list(FEATURE_ORDER)).encode()).hexdigest()[:16]
assert manifest["feature_order_hash"] == expected
```

If `feature_order_hash` does not match current `FEATURE_ORDER`, the dataset was built
with a different feature schema. This is a **hard error** — the dataset cannot be used
for training with the current model contract.

### SHA256 Integrity

For each entry in `sha256`:
- File must exist in the dataset directory
- `hashlib.sha256(file_content).hexdigest()` must match the manifest value
- All files listed in `sha256` must be present; extra files are allowed (e.g., `README.md`)

### Limits

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max `row_count` | 10,000,000 | Prevents accidental multi-GB commits |
| Max file size | 100 MB per file | Git LFS threshold |
| Max dataset directory size | 200 MB total | Repo size hygiene |
| Min `row_count` | 10 | Meaningless datasets below this |

### Path Safety

Same rules as model registry (`src/grinder/ml/onnx/registry.py`):
- No `..` components
- No absolute paths
- No symbolic links resolved outside repo
- Directory name must equal `dataset_id`

---

## 6. Lifecycle and Governance

### How Datasets Connect to Models

```
┌──────────────────────────────────────────────────────────┐
│                 DATASET → MODEL FLOW                      │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  1. BUILD DATASET                                        │
│     python -m scripts.build_dataset                      │
│     → ml/datasets/<dataset_id>/manifest.json             │
│     → ml/datasets/<dataset_id>/data.parquet              │
│                                                           │
│  2. TRAIN MODEL                                          │
│     python -m scripts.train_regime_model                 │
│       --dataset-id <dataset_id>                          │
│     → artifact with manifest.dataset_id = <dataset_id>   │
│                                                           │
│  3. PROMOTE TO REGISTRY                                  │
│     python -m scripts.promote_ml_model                   │
│       --dataset-id <dataset_id>                          │
│     → registry pointer.dataset_id = <dataset_id>         │
│                                                           │
│  4. AUDIT TRAIL                                          │
│     registry.history[].pointer.dataset_id                │
│     → traces back to ml/datasets/<dataset_id>/           │
│                                                           │
└──────────────────────────────────────────────────────────┘
```

### Dataset Lifecycle

| State | Description | Git status |
|-------|-------------|------------|
| **Building** | `build_dataset` running | Not committed |
| **Committed** | PR merged with dataset directory | In main branch |
| **In Use** | Referenced by a model in registry | Pointer exists |
| **Archived** | No longer referenced by any active/shadow model | Can be pruned |

### Promotion Policy

1. **Any dataset referenced by ACTIVE model must be committed to main.**
   The `dataset_id` in the ACTIVE registry pointer must resolve to
   `ml/datasets/<dataset_id>/manifest.json` in the repo.

2. **Dataset changes require a PR** with proof bundle.
   No direct commits to `ml/datasets/`.

3. **Datasets are immutable once committed.**
   To fix a dataset, create a new one with a new `dataset_id`.
   Never modify an existing dataset directory in-place.

4. **Golden datasets** (used for golden test artifacts) are stored in
   `tests/testdata/` and follow the same manifest schema but are not
   required to live under `ml/datasets/`.

### Retention Policy

| Dataset state | Retention |
|---------------|-----------|
| Referenced by ACTIVE pointer | Keep indefinitely |
| Referenced by SHADOW pointer | Keep indefinitely |
| In registry history (last 5) | Keep for audit |
| Unreferenced | Candidate for pruning after 90 days |

Pruning is manual (via PR) — never automated.

---

## 7. Determinism and Reproducibility

### Required Invariants for Rebuild

To reproduce a dataset from scratch, the following must be fixed:

| Factor | Where recorded | Required? |
|--------|---------------|-----------|
| Code version | `determinism.git_sha` | YES for `synthetic` |
| `FEATURE_ORDER` | `feature_order_hash` | YES (always) |
| Random seed | `determinism.seed` | YES for `synthetic` |
| Build command | `determinism.build_command` | YES for `synthetic` |
| Source data | `source_description` | YES for `backtest`/`export` |
| Python + package versions | env fingerprint at build time | RECOMMENDED |

### Rebuild Protocol

```bash
# 1. Checkout exact code version
git checkout <determinism.git_sha>

# 2. Verify feature order matches
python -c "
from grinder.ml.onnx.features import FEATURE_ORDER
import hashlib, json
h = hashlib.sha256(json.dumps(list(FEATURE_ORDER)).encode()).hexdigest()[:16]
print(f'feature_order_hash: {h}')
"

# 3. Run build command
<determinism.build_command>

# 4. Verify SHA256 match
python -m scripts.verify_dataset --path ml/datasets/<dataset_id>
```

### Golden Datasets

A **golden dataset** is one used for golden test artifacts (e.g.,
`tests/testdata/onnx_artifacts/golden_regime/`). Requirements:

- Source: `synthetic` (fully reproducible)
- `determinism.seed` and `determinism.build_command` populated
- Committed to repo (not generated at test time)
- SHA256-locked in manifest

---

## 8. CLI Surfaces (Spec Only)

These CLIs are **not yet implemented**. This section defines the target interface
for future PRs (M8-04a, M8-04b).

### 8.1 build_dataset.py

Builds a dataset from a source and writes the dataset pack.

```bash
# Build from synthetic data generator
python -m scripts.build_dataset \
    --dataset-id synthetic_v1 \
    --source synthetic \
    --seed 42 \
    --n-samples 1000 \
    --out-dir ml/datasets/synthetic_v1

# Build from backtest output
python -m scripts.build_dataset \
    --dataset-id backtest_2025q4_btcusdt \
    --source backtest \
    --replay-dir tests/fixtures/sample_day/ \
    --out-dir ml/datasets/backtest_2025q4_btcusdt

# Build from paper engine export
python -m scripts.build_dataset \
    --dataset-id paper_run_20260217 \
    --source export \
    --log-file var/runs/2026-02-17/run_001/audit.jsonl \
    --out-dir ml/datasets/paper_run_20260217
```

**Output:**
```
Building dataset: synthetic_v1
Source: synthetic (n_samples=1000, seed=42)
Feature order hash: a1b2c3d4e5f67890
Writing data.parquet (1000 rows, 17 columns)
Writing manifest.json
SHA256: data.parquet = abc123def456...
Dataset built: ml/datasets/synthetic_v1/
```

**Exit codes:**
- 0: Success
- 1: Validation error (bad dataset_id, feature order mismatch, etc.)
- 2: I/O error (source not found, disk full, etc.)

### 8.2 verify_dataset.py

Validates a dataset directory against its manifest.

```bash
python -m scripts.verify_dataset \
    --path ml/datasets/synthetic_v1

# Expected output:
# Loading dataset: ml/datasets/synthetic_v1/manifest.json
# Schema version: v1
# Dataset ID: synthetic_v1
# Source: synthetic
# Row count: 1000
# Feature order hash: a1b2c3d4e5f67890 ✓ (matches FEATURE_ORDER)
# SHA256 check:
#   data.parquet: abc123... ✓
#   splits.json:  789012... ✓
# ✓ All checks passed
```

**Checks performed:**
1. `manifest.json` exists and parses
2. `schema_version` is `"v1"`
3. `dataset_id` matches directory name
4. `feature_order_hash` matches current `FEATURE_ORDER`
5. All files in `sha256` map exist and match
6. `row_count` matches actual parquet row count
7. All `feature_order` columns present in parquet
8. All `label_columns` present in parquet
9. Path safety (no traversal, no absolute)

### 8.3 list_datasets.py (Optional)

Lists datasets with their status.

```bash
python -m scripts.list_datasets

# Dataset ID                        Source     Rows    Referenced By
# ─────────────────────────────────  ────────  ──────  ─────────────────
# golden_synthetic_v1                synthetic     50  SHADOW (regime_classifier)
# market_data_2025_q4_btcusdt        backtest   5000  ACTIVE (regime_classifier)
# synthetic_v1                       synthetic   1000  (none)
```

---

## 9. Integration Points

### With train_regime_model.py

Currently `train_regime_model.py` generates synthetic data inline.
Future integration:

```bash
# Current (inline synthetic data):
python -m scripts.train_regime_model --out-dir /tmp/art --dataset-id toy1

# Future (load from feature store):
python -m scripts.train_regime_model --out-dir /tmp/art --dataset-path ml/datasets/synthetic_v1
```

When `--dataset-path` is provided:
1. Load `data.parquet` from the dataset directory
2. Validate `feature_order_hash` matches current `FEATURE_ORDER`
3. Use feature columns + label columns for training
4. Record `dataset_id` in artifact manifest and train report

### With promote_ml_model.py

The promotion CLI already requires `--dataset-id` for ACTIVE stage.
Future enhancement: validate that `dataset_id` resolves to a committed dataset:

```python
# In promote_ml_model.py validation:
dataset_dir = repo_root / "ml" / "datasets" / dataset_id
if stage == "active" and not (dataset_dir / "manifest.json").exists():
    raise ValueError(
        f"ACTIVE promotion requires committed dataset. "
        f"Not found: {dataset_dir}/manifest.json"
    )
```

### With verify_ml_registry.py

Add dataset existence check when verifying registry pointers:

```
$ python -m scripts.verify_ml_registry --path ml/registry/models.json --base-dir .
...
  ACTIVE:
    dataset_id: market_data_2025_q4_btcusdt
    dataset_path: ml/datasets/market_data_2025_q4_btcusdt/
    ✓ Dataset manifest found and valid
```

### With env_fingerprint.py

When building datasets, the env fingerprint should be captured
and stored alongside the manifest for full reproducibility audit.

---

## Appendix A: Feature Order Hash Computation

```python
import hashlib
import json

from grinder.ml.onnx.features import FEATURE_ORDER

feature_order_hash = hashlib.sha256(
    json.dumps(list(FEATURE_ORDER)).encode()
).hexdigest()[:16]

# Current value (15 features):
# Compute at runtime — changes if FEATURE_ORDER changes
```

## Appendix B: Migration from Current State

The current training pipeline (`train_regime_model.py`) generates synthetic
data inline without a dataset pack. Migration path:

1. **M8-04a:** Implement `build_dataset.py` + `verify_dataset.py`
2. **M8-04b:** Build golden dataset from current synthetic generator, commit to `ml/datasets/`
3. **M8-04c:** Update `train_regime_model.py` to accept `--dataset-path`
4. **M8-04d:** Update `promote_ml_model.py` to validate dataset existence for ACTIVE
5. **M8-04e:** Update `verify_ml_registry.py` to check dataset links
