#!/usr/bin/env bash
# Acceptance Packet Paste — generate and insert packet into PR body.
#
# Uses gh api (NOT gh pr edit) to avoid the Projects Classic GraphQL bug.
# Idempotent: re-running replaces the existing packet block, never duplicates.
#
# Usage: ./scripts/acceptance_packet_paste.sh <PR_NUMBER>
#
# Exit codes:
#   0 - Packet generated and pasted successfully
#   1 - Error (packet generation, API call, or post-check failed)
#   2 - Usage error

set -euo pipefail

PR_NUMBER="${1:-}"
if [[ -z "${PR_NUMBER}" ]]; then
  echo "Usage: ./scripts/acceptance_packet_paste.sh <PR_NUMBER>"
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Detect repo
REPO_NWO="$(gh repo view --json nameWithOwner -q '.nameWithOwner')"
if [[ -z "${REPO_NWO}" ]]; then
  echo "ERROR: Could not determine repository (gh repo view failed)."
  exit 1
fi

# Create temp files via mktemp (safe for parallel runs)
TMPDIR_PASTE="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_PASTE}"' EXIT

PACKET_FILE="${TMPDIR_PASTE}/packet.txt"
BODY_FILE="${TMPDIR_PASTE}/body.txt"
UPDATE_FILE="${TMPDIR_PASTE}/update.json"

# --- Step 1: Generate acceptance packet ---
echo "==> Generating acceptance packet for PR #${PR_NUMBER}..."
set +e
bash "${SCRIPT_DIR}/acceptance_packet.sh" "${PR_NUMBER}" > "${PACKET_FILE}" 2>&1
PACKET_EXIT=$?
set -e

if [[ ${PACKET_EXIT} -ne 0 ]]; then
  echo "ERROR: acceptance_packet.sh failed (exit ${PACKET_EXIT})."
  echo "Packet output saved to: ${PACKET_FILE}"
  echo "--- last 20 lines ---"
  tail -20 "${PACKET_FILE}"
  exit 1
fi

echo "==> Packet generated ($(wc -c < "${PACKET_FILE}") bytes)."

# --- Step 2: Fetch current PR body ---
echo "==> Fetching current PR body..."
gh api "repos/${REPO_NWO}/pulls/${PR_NUMBER}" -q '.body // ""' > "${BODY_FILE}"
echo "==> Current body: $(wc -c < "${BODY_FILE}") bytes."

# --- Step 3: Build updated body (Python handles escaping + JSON) ---
echo "==> Building updated body..."
python3 << 'PYEOF'
import json, os, re, sys

body_file = os.environ["BODY_FILE"]
packet_file = os.environ["PACKET_FILE"]
update_file = os.environ["UPDATE_FILE"]

with open(body_file, "r") as f:
    current_body = f.read()

with open(packet_file, "r") as f:
    packet = f.read()

# Build the acceptance packet block
new_block = (
    "<!-- ACCEPTANCE_PACKET_START -->\n\n"
    "<details><summary>Acceptance Packet (verbatim)</summary>\n\n"
    "```\n"
    + packet
    + "\n```\n\n"
    "</details>\n\n"
    "<!-- ACCEPTANCE_PACKET_END -->"
)

START_MARKER = "<!-- ACCEPTANCE_PACKET_START -->"
END_MARKER = "<!-- ACCEPTANCE_PACKET_END -->"

if START_MARKER in current_body:
    # Replace existing block (idempotent)
    pattern = re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER)
    new_body = re.sub(pattern, new_block, current_body, count=1, flags=re.DOTALL)
    print("Mode: REPLACE (existing block found)")
else:
    # Append
    new_body = current_body.rstrip() + "\n\n" + new_block
    print("Mode: APPEND (no existing block)")

with open(update_file, "w") as f:
    json.dump({"body": new_body}, f)

print(f"Update JSON: {len(new_body)} bytes body written to {update_file}")
PYEOF

# --- Step 4: PATCH via gh api ---
echo "==> Updating PR body via gh api..."
PATCH_OUTPUT="$(gh api "repos/${REPO_NWO}/pulls/${PR_NUMBER}" -X PATCH --input "${UPDATE_FILE}" -q '.html_url' 2>&1)" || {
  echo "ERROR: gh api PATCH failed."
  echo "Output: ${PATCH_OUTPUT}"
  echo ""
  echo "Hint: you can retry manually:"
  echo "  gh api repos/${REPO_NWO}/pulls/${PR_NUMBER} -X PATCH --input ${UPDATE_FILE}"
  echo "  (temp files preserved until script exits)"
  # Don't exit yet — let trap clean up. Re-raise.
  exit 1
}
echo "==> Updated: ${PATCH_OUTPUT}"

# --- Step 5: Post-check ---
echo "==> Post-check: verifying markers in updated body..."
UPDATED_BODY="$(gh api "repos/${REPO_NWO}/pulls/${PR_NUMBER}" -q '.body // ""')"

PACKET_MARKER_COUNT="$(echo "${UPDATED_BODY}" | grep -c "== ACCEPTANCE PACKET:" || true)"
HAS_START="$(echo "${UPDATED_BODY}" | grep -c "<!-- ACCEPTANCE_PACKET_START -->" || true)"
HAS_END="$(echo "${UPDATED_BODY}" | grep -c "<!-- ACCEPTANCE_PACKET_END -->" || true)"

if [[ ${PACKET_MARKER_COUNT} -ge 1 && ${HAS_START} -ge 1 && ${HAS_END} -ge 1 ]]; then
  echo "OK: ${PACKET_MARKER_COUNT} acceptance packet markers found."
  echo "OK: START/END comment markers present."
  echo "==> Done. PR #${PR_NUMBER} body updated successfully."
  exit 0
else
  echo "ERROR: Post-check failed."
  echo "  Packet markers: ${PACKET_MARKER_COUNT} (expected >=1)"
  echo "  START marker: ${HAS_START} (expected >=1)"
  echo "  END marker: ${HAS_END} (expected >=1)"
  echo ""
  echo "Hint: packet content saved at ${PACKET_FILE}"
  echo "Hint: paste manually into PR body."
  exit 1
fi
