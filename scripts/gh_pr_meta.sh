#!/usr/bin/env bash
# gh_pr_meta.sh — resilient PR metadata retrieval via gh api
#
# Replaces `gh pr view <N> --json ...` which fails when GitHub Projects
# (classic) is enabled on the repo (GraphQL deprecation error).
#
# Usage:
#   ./scripts/gh_pr_meta.sh <PR_NUMBER>
#   ./scripts/gh_pr_meta.sh <PR_NUMBER> --jq '<jq_expr>'
#
# Outputs JSON to stdout (or jq-filtered result if --jq is specified).
# Exit codes:
#   0 — success
#   1 — API error or missing PR
#   2 — usage error

set -euo pipefail

PR_NUMBER="${1:-}"
JQ_EXPR=""

if [[ -z "${PR_NUMBER}" ]]; then
  echo "Usage: ./scripts/gh_pr_meta.sh <PR_NUMBER> [--jq '<expr>']" >&2
  exit 2
fi

shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq)
      JQ_EXPR="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

# Auto-detect repo NWO (works in both local and CI)
REPO_NWO="$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || true)"
if [[ -z "${REPO_NWO}" ]]; then
  echo "ERROR: could not detect repo (gh repo view failed)" >&2
  exit 1
fi

if [[ -n "${JQ_EXPR}" ]]; then
  gh api "repos/${REPO_NWO}/pulls/${PR_NUMBER}" --jq "${JQ_EXPR}"
else
  gh api "repos/${REPO_NWO}/pulls/${PR_NUMBER}" --jq '{
    number: .number,
    title: .title,
    state: .state,
    merged: .merged,
    merged_at: .merged_at,
    merge_commit_sha: .merge_commit_sha,
    base: .base.ref,
    head: .head.ref,
    head_sha: .head.sha,
    url: .html_url
  }'
fi
