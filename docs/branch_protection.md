# Branch Protection: `main`

> **Last updated:** 2026-02-17
>
> **Applied via:** GitHub API (`gh api repos/bnzr-hub/grinder/branches/main/protection`)

---

## Rules

| Setting | Value | Notes |
|---------|-------|-------|
| Require PR reviews | **0** | Single-contributor repo; self-approve not possible on GitHub |
| Dismiss stale approvals | **ON** | New push invalidates old approval |
| Require conversation resolution | **OFF** | Disabled (single-contributor) |
| Require status checks to pass | **ON** | See list below |
| Require branches up to date | **ON** (strict) | Must rebase/merge before merge |
| Include administrators | **ON** | Admins follow same rules |
| Required linear history | **ON** | Squash merge only |
| Allow force pushes | **OFF** | |
| Allow deletions | **OFF** | |

---

## Check Classification

All CI checks are classified into exactly one of two classes.
**Only Class A checks may be added to GitHub required status checks.**

### Class A — Required / Always-run (5)

These run on **every PR** regardless of changed files. All must pass before merge.
They are registered as **required status checks** in branch protection.

| Check | What it validates |
|-------|-------------------|
| `acceptance-packet` | Runs `acceptance_packet.sh`, produces verbatim proof artifact |
| `checks` | ruff + mypy + pytest full suite + env fingerprint |
| `guard` | PR body contains `## Proof` section with real command output |
| `proof-guard` | PR body contains verbatim acceptance packet output |
| `secret-scan` | Scans for leaked secrets/credentials |

### Class B — Path-filtered / Conditional (6)

These run only when relevant code paths change. They are **NOT** registered as
required status checks in GitHub branch protection.

**Rationale:** GitHub treats a never-triggered required check as "not passing".
If a Class B check is marked required, docs-only PRs are permanently blocked
because the check never starts and therefore never reports a status.

| Check | Triggers on | What it validates |
|-------|-------------|-------------------|
| `determinism-suite` | `src/`, `scripts/`, `tests/` | Paper engine fixture replay determinism |
| `docker-smoke` | `Dockerfile`, `src/`, `scripts/`, `monitoring/` | Docker build + container smoke test |
| `ha-mismatch-smoke` | `src/`, `docker-compose.ha.yml` | HA mismatch detection smoke test |
| `ha-smoke` | `src/`, `docker-compose.ha.yml` | HA leader election smoke test |
| `observability-smoke` | `src/`, `monitoring/` | Metrics/logging smoke test |
| `soak-gate` | `src/`, `scripts/`, `tests/fixtures/` | Soak test gate (long-running stability) |

> **Upgrading a Class B check to Class A:** The workflow must first be changed to
> trigger on all PRs (remove `paths:` filter or add `docs/**` to triggers). Only
> then may it be added to required status checks. Verify with a docs-only test PR.

---

## Workflow Change Policy

Any PR that modifies `.github/workflows/*` **must**:

1. State which checks are affected and their class (A or B) in the PR body.
2. Prove that all Class A checks still trigger on every PR type (including docs-only).
   - If a `paths:` filter is added to a Class A workflow, it is **reclassified to Class B**
     and must be removed from required status checks before merge.
3. Update this document if any check is added, removed, renamed, or reclassified.
4. Include the standard proof bundle (gates + env fingerprint).

---

## Env Fingerprint

Every `make gates` run and every Class A CI run prints an environment fingerprint
before tests execute. This captures:

- Python executable path and version
- Whether running inside a virtualenv
- Installed versions of key ML packages (`onnx`, `onnxruntime`, `numpy`,
  `scikit-learn`, `skl2onnx`) or `MISSING` if not installed

This prevents "red because wrong environment" failures from going undiagnosed.
Run manually: `python -m scripts.env_fingerprint`

---

## How to modify

### Adding a new required check (Class A)

1. Add the workflow job to `.github/workflows/`
2. Verify it runs on **all** PR events (no `paths:` filter)
3. Add to branch protection via API:
   ```bash
   # Get current checks
   gh api repos/bnzr-hub/grinder/branches/main/protection/required_status_checks \
     --jq '.checks[].context'

   # Update (must send ALL checks, not just the new one)
   gh api repos/bnzr-hub/grinder/branches/main/protection -X PUT --input protection.json
   ```
4. Update this document (add to Class A table)

### Adding a new conditional check (Class B)

1. Add the workflow job with appropriate `paths:` filter
2. **Do NOT add to required status checks**
3. Update this document (add to Class B table)

### Renaming a check

If a workflow job is renamed:

1. The old check name will fail (missing from CI output)
2. PRs will be blocked until the protection rule is updated
3. Update the check name in branch protection via API
4. Update this document

### Emergency bypass

If a critical fix is blocked by a flaky check:

1. **Do NOT disable branch protection**
2. Identify the flaky check and fix it
3. If truly urgent: repo admin can use `--admin` flag with `gh pr merge`, but this is auditable and should be documented in the PR body
4. File a follow-up issue for the flaky check

---

## Verification

```bash
# Verify all settings
gh api repos/bnzr-hub/grinder/branches/main/protection --jq '{
  enforce_admins: .enforce_admins.enabled,
  required_reviews: .required_pull_request_reviews.required_approving_review_count,
  dismiss_stale: .required_pull_request_reviews.dismiss_stale_reviews,
  conversation_resolution: .required_conversation_resolution.enabled,
  linear_history: .required_linear_history.enabled,
  strict_up_to_date: .required_status_checks.strict,
  force_push: .allow_force_pushes.enabled,
  deletions: .allow_deletions.enabled,
  required_checks_count: (.required_status_checks.checks | length)
}'

# List required checks (must be exactly Class A)
gh api repos/bnzr-hub/grinder/branches/main/protection/required_status_checks \
  --jq '.checks[].context' | sort

# Run env fingerprint
python -m scripts.env_fingerprint
```
