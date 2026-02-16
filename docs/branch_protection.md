# Branch Protection: `main`

> **Last updated:** 2026-02-16
>
> **Applied via:** GitHub API (`gh api repos/bnzr-hub/grinder/branches/main/protection`)

---

## Rules

| Setting | Value | Notes |
|---------|-------|-------|
| Require PR reviews | **1 minimum** | No direct pushes to main |
| Dismiss stale approvals | **ON** | New push invalidates old approval |
| Require conversation resolution | **ON** | All threads must be resolved |
| Require status checks to pass | **ON** | See list below |
| Require branches up to date | **ON** (strict) | Must rebase/merge before merge |
| Include administrators | **ON** | Admins follow same rules |
| Required linear history | **ON** | Squash merge only |
| Allow force pushes | **OFF** | |
| Allow deletions | **OFF** | |

---

## Required Status Checks (5 required + 6 path-filtered)

### Always Required (5)

These run on **every PR** regardless of changed files. All must pass before merge.

| Check | What it validates |
|-------|-------------------|
| `acceptance-packet` | Runs `acceptance_packet.sh`, produces verbatim proof artifact |
| `checks` | ruff + mypy + pytest full suite |
| `guard` | PR body contains `## Proof` section with real command output |
| `proof-guard` | PR body contains verbatim acceptance packet output |
| `secret-scan` | Scans for leaked secrets/credentials |

### Path-Filtered (6)

These run only when relevant code paths change. **Not GitHub-required** (path-filtered
checks that don't trigger would block docs-only PRs). They still run and must pass on
code PRs — just not enforced at the branch protection level.

| Check | Triggers on | What it validates |
|-------|-------------|-------------------|
| `determinism-suite` | `src/`, `scripts/`, `tests/` | Paper engine fixture replay determinism |
| `docker-smoke` | `Dockerfile`, `src/`, `scripts/`, `monitoring/` | Docker build + container smoke test |
| `ha-mismatch-smoke` | `src/`, `docker-compose.ha.yml` | HA mismatch detection smoke test |
| `ha-smoke` | `src/`, `docker-compose.ha.yml` | HA leader election smoke test |
| `observability-smoke` | `src/`, `monitoring/` | Metrics/logging smoke test |
| `soak-gate` | `src/`, `scripts/`, `tests/fixtures/` | Soak test gate (long-running stability) |

> **Note:** If the 6 path-filtered checks are needed as hard requirements, their workflow
> files must add `docs/**` to their `paths:` triggers (or use `paths-ignore:` instead).
> This is a trade-off: fewer CI runs vs stronger enforcement.

---

## How to modify

### Adding a new required check

1. Add the workflow job to `.github/workflows/`
2. Verify it runs on PR events and produces a named check
3. Add to branch protection via API:
   ```bash
   # Get current checks
   gh api repos/bnzr-hub/grinder/branches/main/protection/required_status_checks \
     --jq '.checks[].context'

   # Update (must send ALL checks, not just the new one)
   gh api repos/bnzr-hub/grinder/branches/main/protection -X PUT --input protection.json
   ```
4. Update this document

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

# List required checks
gh api repos/bnzr-hub/grinder/branches/main/protection/required_status_checks \
  --jq '.checks[].context' | sort
```
