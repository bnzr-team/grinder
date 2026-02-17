# Feature Store Datasets (M8-04e)

This runbook describes how to build, verify, and use dataset artifacts for ML training.

## 1. Dataset artifact layout (spec v1)

A dataset artifact lives under:

```
ml/datasets/<dataset_id>/
  data.parquet
  manifest.json
```

The manifest MUST conform to Feature Store spec v1 and MUST pass `verify_dataset`.

## 2. Build a dataset (synthetic)

Example (writes to `ml/datasets/<dataset_id>/`):

```bash
.venv/bin/python -m scripts.build_dataset \
  --out-dir ml/datasets \
  --dataset-id demo_ds \
  --source synthetic \
  --rows 200 \
  --seed 42 \
  -v
```

Notes:

- Use a stable `dataset_id` (no path separators, no `..`, not absolute).
- Use `--seed` for reproducible synthetic datasets.
- `--force` overwrites an existing dataset directory (fail-closed without it).

## 3. Verify a dataset

```bash
.venv/bin/python -m scripts.verify_dataset \
  --path ml/datasets/demo_ds/manifest.json \
  --base-dir ml/datasets \
  -v
```

Expected:

- Exit code `0` on success
- Exit code `1` if any check fails (fail-closed)

## 4. Common failures and fixes

### 4.1 SHA256 mismatch

Cause: `data.parquet` was modified without updating the manifest.
Fix: rebuild the dataset with `build_dataset` or regenerate the manifest.

### 4.2 Feature order mismatch

Cause: columns do not match SSOT `FEATURE_ORDER`.
Fix: rebuild using the SSOT `FEATURE_ORDER` (builder does this by default).

### 4.3 Size limit exceeded

Cause: dataset directory exceeds the configured maximum size.
Fix: reduce rows, use a smaller dataset, or adjust limits only via a spec change + tests.

### 4.4 Dataset path safety errors

Cause: invalid `dataset_id` (absolute path or traversal).
Fix: use a clean identifier (e.g., `demo_ds`, `golden_tiny_v1`).

## 5. Using datasets in training (M8-04c)

Training requires `--dataset-manifest`:

```bash
.venv/bin/python -m scripts.train_regime_model \
  --dataset-manifest ml/datasets/demo_ds/manifest.json \
  ...other args...
```

The resulting ONNX model artifact manifest includes `dataset_id`, enabling traceability:
dataset -> model -> registry/promotion.

## 6. Promotion guard (M8-04d)

ACTIVE promotion is fail-closed if dataset verification fails:

- dataset directory must exist
- manifest.json must exist
- `verify_dataset` must PASS (SHA256 integrity, feature_order, schema)
- optional CLI `--dataset-id` must match the model manifest `dataset_id`

See also: `docs/runbooks/19_ML_MODEL_PROMOTION.md`
