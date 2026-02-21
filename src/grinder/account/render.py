"""Deterministic rendering and sha256 for AccountSnapshot (Launch-15).

Invariants enforced (Spec 15.5):
- I1: Deterministic serialization (same input -> byte-identical JSON)
- I2: Round-trip equality (snapshot == load(render(snapshot)))
- I3: Sha256 stability (same input -> same hash)

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.5)
"""

from __future__ import annotations

import hashlib
import json

from grinder.account.contracts import AccountSnapshot


def render_snapshot(snapshot: AccountSnapshot) -> str:
    """Render AccountSnapshot to deterministic JSON string.

    Uses sort_keys + separators for byte-identical output across runs.
    """
    return json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))


def load_snapshot(json_str: str) -> AccountSnapshot:
    """Load AccountSnapshot from JSON string."""
    return AccountSnapshot.from_dict(json.loads(json_str))


def snapshot_sha256(snapshot: AccountSnapshot) -> str:
    """Compute sha256 hex digest of deterministic JSON rendering."""
    rendered = render_snapshot(snapshot)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
