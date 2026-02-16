# ML Model Registry

SSOT for ML model artifacts and promotion pointers.

## Structure

- `models.json` - Registry schema (v1)
  - Pointers to artifact directories by stage (shadow/active)
  - Audit trail via `history[]` array
  - Version controlled via Git

## Schema v1

```json
{
  "schema_version": "v1",
  "models": {
    "<model_name>": {
      "shadow": {
        "artifact_dir": "relative/path/to/artifact",
        "artifact_id": "human_readable_id",
        "git_sha": "40-char-hex-or-null",
        "dataset_id": "optional_dataset_id"
      },
      "active": null,
      "history": [...]
    }
  }
}
```

## Validation

Run validation CLI:
```bash
python -m scripts.verify_ml_registry --path ml/registry/models.json
```

## Promotion

Promote model by PR:
1. Update pointer in `models.json` (e.g., `active` → new artifact_dir)
2. Append entry to `history[]` with timestamp and reason
3. Create PR with proof bundle
4. CI validates all referenced artifacts

## Rollback

Revert `models.json` to previous commit:
```bash
git checkout <prev-commit> ml/registry/models.json
```

## Related

- [docs/runbooks/19_ML_MODEL_PROMOTION.md](../../docs/runbooks/19_ML_MODEL_PROMOTION.md) - Promotion runbook
- [docs/12_ML_SPEC.md](../../docs/12_ML_SPEC.md) - ML architecture spec
