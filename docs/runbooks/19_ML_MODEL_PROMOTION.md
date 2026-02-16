# Runbook: ML Model Promotion

> **Purpose:** Safe promotion of ML models from shadow → staging → active with full audit trail
>
> **Audience:** ML Engineers, DevOps, Production Support
>
> **Status:** M8-03c-3 Implementation
>
> **Last Updated:** 2026-02-16

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Stage Lifecycle](#stage-lifecycle)
4. [Promotion Workflow](#promotion-workflow)
5. [Safety Checks](#safety-checks)
6. [Rollback Procedures](#rollback-procedures)
7. [Troubleshooting](#troubleshooting)
8. [Emergency Contacts](#emergency-contacts)

---

## Overview

The ML model promotion system provides a controlled, audited pathway for moving trained models from validation (SHADOW) to production (ACTIVE) deployment.

### Key Principles

- **Fail-closed**: ACTIVE mode requires full metadata (git_sha, dataset_id, timestamp)
- **Audit trail**: Every promotion creates an immutable history[] entry
- **Path safety**: No directory traversal or absolute paths allowed
- **Artifact integrity**: All models verified before promotion

### Promotion Tool

\`\`\`bash
scripts/promote_ml_model.py
\`\`\`

- Located in repository root
- Requires Python 3.10+
- Uses grinder.ml.onnx.registry module

---

## Prerequisites

### Environment Setup

1. **Git working tree must be clean**
   \`\`\`bash
   git status
   # Ensure no uncommitted changes
   \`\`\`

2. **Set promotion actor (optional)**
   \`\`\`bash
   export ML_PROMOTION_ACTOR="alice@grinder.dev"
   # Falls back to git config user.email if not set
   \`\`\`

3. **Verify artifact integrity**
   \`\`\`bash
   python -m scripts.verify_ml_registry \\
       --path ml/registry/models.json \\
       --base-dir .
   \`\`\`

### Required Metadata for ACTIVE Promotion

| Field | Format | Example | Notes |
|-------|--------|---------|-------|
| \`git_sha\` | 40-char hex | \`93e64007df...\` | Commit SHA of training code |
| \`dataset_id\` | Non-empty string | \`market_data_2026_q1_btcusdt\` | Training dataset identifier |
| \`artifact_dir\` | Relative path | \`ml/artifacts/regime_v1\` | No \`..\`, no absolute paths |
| \`artifact_id\` | Human-readable | \`regime_v1_prod\` | Semantic version identifier |

---

## Stage Lifecycle

\`\`\`
┌──────────────────────────────────────────────────────────────────┐
│                     PROMOTION STAGES                              │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  1. SHADOW (Validation)                                          │
│     ├── Purpose: Offline validation, A/B testing                 │
│     ├── Impact: No production trading decisions                  │
│     ├── Duration: 7-14 days recommended                          │
│     └── Requirements: artifact_dir, artifact_id                  │
│                                                                   │
│  2. STAGING (Optional Pre-Production)                            │
│     ├── Purpose: Integration testing, canary deployment          │
│     ├── Impact: Limited production exposure (1-5% capital)       │
│     ├── Duration: 2-7 days                                       │
│     └── Requirements: artifact_dir, artifact_id                  │
│                                                                   │
│  3. ACTIVE (Production)                                          │
│     ├── Purpose: Full production deployment                      │
│     ├── Impact: Affects real trading decisions                   │
│     ├── Duration: Until next model promoted                      │
│     └── Requirements: artifact_dir, artifact_id, git_sha,        │
│                       dataset_id, promoted_at_utc (auto)         │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
\`\`\`

---

## Promotion Workflow

### Step 1: Shadow Deployment (Validation)

**Goal:** Deploy new model for offline validation without production impact

\`\`\`bash
# 1. Verify artifact exists and is valid
ls -la ml/artifacts/regime_v2/
cat ml/artifacts/regime_v2/manifest.json

# 2. Dry-run promotion to preview changes
python -m scripts.promote_ml_model \\
    --dry-run \\
    --model regime_classifier \\
    --stage shadow \\
    --artifact-dir ml/artifacts/regime_v2 \\
    --artifact-id regime_v2_candidate \\
    --dataset-id market_data_2026_q1_multi \\
    --notes "New candidate model trained on Q1 2026 data"

# 3. Review dry-run output (JSON diff)
# Verify:
#   - artifact_dir is correct
#   - artifact_id is semantic
#   - dataset_id matches training data
#   - history[] entry looks correct

# 4. Execute promotion (no --dry-run)
python -m scripts.promote_ml_model \\
    --model regime_classifier \\
    --stage shadow \\
    --artifact-dir ml/artifacts/regime_v2 \\
    --artifact-id regime_v2_candidate \\
    --dataset-id market_data_2026_q1_multi \\
    --notes "New candidate model trained on Q1 2026 data"

# 5. Verify registry was updated
python -m scripts.verify_ml_registry \\
    --path ml/registry/models.json \\
    --base-dir .

# 6. Commit registry changes
git add ml/registry/models.json
git commit -m "feat(ml): Promote regime_v2_candidate to SHADOW"
git push origin main
\`\`\`

**Validation Period:** 7-14 days
- Monitor shadow metrics vs baseline
- Compare predictions to actual outcomes
- Check for drift in feature distributions

### Step 2: Staging Deployment (Optional)

**Goal:** Limited production exposure for integration testing

\`\`\`bash
# Same process as SHADOW, but with stage=staging
python -m scripts.promote_ml_model \\
    --model regime_classifier \\
    --stage staging \\
    --artifact-dir ml/artifacts/regime_v2 \\
    --artifact-id regime_v2_staging \\
    --dataset-id market_data_2026_q1_multi \\
    --notes "Staging deployment for canary testing"
\`\`\`

**Canary Configuration:**
- Allocate 1-5% of trading capital to staging model
- Monitor performance metrics closely
- Compare to shadow and active models

### Step 3: Active Deployment (Production)

**Goal:** Promote validated model to full production

**CRITICAL**: ACTIVE promotion requires additional metadata

\`\`\`bash
# 1. Get current git SHA (training code commit)
GIT_SHA=\$(git rev-parse HEAD)
echo "Using git SHA: \$GIT_SHA"

# 2. Dry-run with ACTIVE requirements
python -m scripts.promote_ml_model \\
    --dry-run \\
    --model regime_classifier \\
    --stage active \\
    --artifact-dir ml/artifacts/regime_v2 \\
    --artifact-id regime_v2_prod \\
    --dataset-id market_data_2026_q1_multi \\
    --git-sha \$GIT_SHA \\
    --reason "SHADOW validation passed: 95% accuracy over 14 days" \\
    --notes "First production deployment of regime_v2"

# 3. Review dry-run output
# VERIFY:
#   - git_sha matches training code commit
#   - dataset_id matches training dataset
#   - artifact passes integrity check
#   - history[] preserves previous ACTIVE pointer

# 4. Execute ACTIVE promotion
python -m scripts.promote_ml_model \\
    --model regime_classifier \\
    --stage active \\
    --artifact-dir ml/artifacts/regime_v2 \\
    --artifact-id regime_v2_prod \\
    --dataset-id market_data_2026_q1_multi \\
    --git-sha \$GIT_SHA \\
    --reason "SHADOW validation passed: 95% accuracy over 14 days" \\
    --notes "First production deployment of regime_v2"

# 5. Verify and commit
python -m scripts.verify_ml_registry \\
    --path ml/registry/models.json \\
    --base-dir .

git add ml/registry/models.json
git commit -m "feat(ml): Promote regime_v2 to ACTIVE (production)"
git push origin main

# 6. Monitor production metrics
# - Check ml_regime injected into policy_features
# - Monitor prediction accuracy vs baseline
# - Watch for performance degradation
\`\`\`

---

## Safety Checks

### Pre-Promotion Checklist

- [ ] Artifact exists and passes integrity check
- [ ] Dataset ID matches training data provenance
- [ ] Git SHA matches training code commit (ACTIVE only)
- [ ] Dry-run output reviewed and approved
- [ ] Registry verification passes
- [ ] Git working tree is clean
- [ ] Promotion reason documented

### Post-Promotion Verification

\`\`\`bash
# 1. Verify registry loads without errors
python -c "
from grinder.ml.onnx.registry import ModelRegistry
registry = ModelRegistry.load('ml/registry/models.json')
pointer = registry.get_stage_pointer('regime_classifier', 'active')
print(f'ACTIVE artifact: {pointer.artifact_id if pointer else None}')
print(f'Git SHA: {pointer.git_sha if pointer else None}')
"

# 2. Check history[] audit trail
python -c "
from grinder.ml.onnx.registry import ModelRegistry
import json
registry = ModelRegistry.load('ml/registry/models.json')
history = registry.history.get('regime_classifier', [])
print(f'History entries: {len(history)}')
if history:
    latest = history[0]
    print(f'Latest promotion: {latest.from_stage} → {latest.to_stage}')
    print(f'Actor: {latest.actor}')
    print(f'Timestamp: {latest.ts_utc}')
"

# 3. Test artifact loading in runtime
python -c "
from grinder.ml.onnx.artifact import load_artifact
from pathlib import Path
artifact = load_artifact(Path('ml/artifacts/regime_v2'))
print(f'Artifact files: {len(artifact.manifest.sha256)}')
print(f'Model SHA256: {artifact.manifest.model_sha256}')
"
\`\`\`

---

## Rollback Procedures

### Emergency Rollback (Production Incident)

**Scenario:** ACTIVE model is causing production issues (accuracy drop, crashes, etc.)

**Action:**

\`\`\`bash
# 1. Check history[] for previous ACTIVE pointer
python -c "
from grinder.ml.onnx.registry import ModelRegistry
registry = ModelRegistry.load('ml/registry/models.json')
history = registry.history.get('regime_classifier', [])
for i, event in enumerate(history[:5]):
    if event.to_stage == 'active':
        print(f'[{i}] {event.ts_utc}: {event.pointer.artifact_id}')
        print(f'    git_sha: {event.pointer.git_sha}')
        print(f'    dataset_id: {event.pointer.dataset_id}')
"

# 2. Re-promote previous ACTIVE artifact
python -m scripts.promote_ml_model \\
    --model regime_classifier \\
    --stage active \\
    --artifact-dir <PREVIOUS_ARTIFACT_DIR> \\
    --artifact-id <PREVIOUS_ARTIFACT_ID>_rollback \\
    --dataset-id <PREVIOUS_DATASET_ID> \\
    --git-sha <PREVIOUS_GIT_SHA> \\
    --reason "EMERGENCY ROLLBACK: Production incident #123" \\
    --notes "Rolled back to previous stable version"

# 3. Verify rollback
python -m scripts.verify_ml_registry \\
    --path ml/registry/models.json \\
    --base-dir .

# 4. Commit and deploy
git add ml/registry/models.json
git commit -m "fix(ml): Emergency rollback of regime_classifier (incident #123)"
git push origin main

# 5. Restart PaperEngine to load rolled-back model
# (Implementation-specific: systemctl restart, kubectl rollout, etc.)
\`\`\`

---

## Troubleshooting

### Issue: "ACTIVE promotion requires --git-sha"

**Cause:** Attempting to promote to ACTIVE without required metadata

**Solution:**
\`\`\`bash
# Get current git SHA
git rev-parse HEAD

# Re-run with --git-sha
python -m scripts.promote_ml_model \\
    --model regime_classifier \\
    --stage active \\
    --git-sha <40-CHAR-HEX-SHA> \\
    ...
\`\`\`

### Issue: "Path traversal not allowed"

**Cause:** Artifact directory contains ".." or absolute path

**Solution:**
\`\`\`bash
# Use relative path from registry parent (usually repo root)
# ❌ Bad: --artifact-dir ../../../etc/passwd
# ❌ Bad: --artifact-dir /absolute/path
# ✓ Good: --artifact-dir ml/artifacts/regime_v2
\`\`\`

### Issue: "Artifact directory not found"

**Cause:** Artifact directory doesn't exist or path is wrong

**Solution:**
\`\`\`bash
# Verify artifact exists
ls -la ml/artifacts/regime_v2/

# Check manifest.json
cat ml/artifacts/regime_v2/manifest.json

# Ensure artifact_dir is relative to --base-dir
python -m scripts.verify_ml_registry \\
    --path ml/registry/models.json \\
    --base-dir .
\`\`\`

---

## Emergency Contacts

| Role | Contact | Responsibilities |
|------|---------|------------------|
| ML Engineer | ml-team@grinder.dev | Model training, validation |
| DevOps | devops@grinder.dev | Registry deployment, rollback |
| Production Support | support@grinder.dev | Incident response, monitoring |
| On-Call | oncall@grinder.dev | 24/7 emergency escalation |

---

**Document Version:** 1.0
**Last Review:** 2026-02-16
**Next Review:** 2026-03-16
