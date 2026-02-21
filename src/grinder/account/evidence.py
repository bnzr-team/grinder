"""Env-gated evidence artifact writer for AccountSyncer (Launch-15 PR2).

Writes deterministic evidence bundles for account sync snapshots.
Follows the same pattern as fsm_evidence.py (Launch-13 PR5).

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.6)

Design:
- Safe-by-default: no files written unless GRINDER_ACCOUNT_SYNC_EVIDENCE is truthy.
- Atomic file writes (tmp + os.replace).
- Deterministic JSON rendering (via render.py).
- sha256sums for tamper detection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from grinder.account.render import render_snapshot
from grinder.env_parse import parse_bool

if TYPE_CHECKING:
    from grinder.account.contracts import AccountSnapshot
    from grinder.account.syncer import Mismatch

logger = logging.getLogger(__name__)

ENV_ENABLE = "GRINDER_ACCOUNT_SYNC_EVIDENCE"
ENV_ARTIFACT_DIR = "GRINDER_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR = ".artifacts"


def should_write_evidence() -> bool:
    """Check if evidence writing is enabled via env var.

    Safe-by-default: returns False if env var is unset, empty, or non-truthy.

    Uses :func:`grinder.env_parse.parse_bool` (SSOT for truthy/falsey).
    """
    return parse_bool(ENV_ENABLE, default=False, strict=False)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _sha256_hex(text: str) -> str:
    """Compute sha256 hex digest of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence_dir() -> Path:
    """Build timestamped evidence directory path."""
    base = os.environ.get(ENV_ARTIFACT_DIR, DEFAULT_ARTIFACT_DIR)
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(base) / "account_sync" / ts


def write_evidence_bundle(
    snapshot: AccountSnapshot,
    mismatches: list[Mismatch],
) -> Path | None:
    """Write full evidence bundle for an account sync cycle.

    Files written:
        account_snapshot.json  -- Full snapshot (canonical JSON)
        positions.json         -- Positions only
        open_orders.json       -- Open orders only
        mismatches.json        -- Detected mismatches (if any)
        summary.txt            -- Human-readable evidence block
        sha256sums.txt         -- sha256 of all artifact files

    Returns:
        Evidence directory path, or None if writing is disabled/failed.
    """
    if not should_write_evidence():
        return None

    try:
        return _write_bundle_inner(snapshot, mismatches)
    except OSError:
        logger.warning(
            "Failed to write account sync evidence",
            exc_info=True,
        )
        return None


def _write_bundle_inner(
    snapshot: AccountSnapshot,
    mismatches: list[Mismatch],
) -> Path:
    """Inner bundle writer (no env check, raises on failure)."""
    out_dir = _evidence_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. account_snapshot.json (canonical via render_snapshot)
    snapshot_json = render_snapshot(snapshot)
    _atomic_write_text(out_dir / "account_snapshot.json", snapshot_json + "\n")

    # 2. positions.json
    positions_data = [p.to_dict() for p in snapshot.positions]
    positions_json = json.dumps(positions_data, sort_keys=True, separators=(",", ":"))
    _atomic_write_text(out_dir / "positions.json", positions_json + "\n")

    # 3. open_orders.json
    orders_data = [o.to_dict() for o in snapshot.open_orders]
    orders_json = json.dumps(orders_data, sort_keys=True, separators=(",", ":"))
    _atomic_write_text(out_dir / "open_orders.json", orders_json + "\n")

    # 4. mismatches.json
    mismatches_data = [m.to_dict() for m in mismatches]
    mismatches_json = json.dumps(mismatches_data, sort_keys=True, separators=(",", ":"))
    _atomic_write_text(out_dir / "mismatches.json", mismatches_json + "\n")

    # 5. summary.txt
    summary_lines = [
        "=== Account Sync Evidence ===",
        f"ts: {snapshot.ts}",
        f"source: {snapshot.source}",
        f"positions: {len(snapshot.positions)}",
        f"open_orders: {len(snapshot.open_orders)}",
        f"mismatches: {len(mismatches)}",
    ]
    if mismatches:
        summary_lines.append("mismatch_details:")
        for m in mismatches:
            summary_lines.append(f"  - [{m.rule}] {m.detail}")
    summary_lines.append(f"snapshot_sha256: {_sha256_hex(snapshot_json)}")
    summary_lines.append("")  # trailing newline
    summary_text = "\n".join(summary_lines)
    _atomic_write_text(out_dir / "summary.txt", summary_text)

    # 6. sha256sums.txt
    artifact_names = [
        "account_snapshot.json",
        "positions.json",
        "open_orders.json",
        "mismatches.json",
        "summary.txt",
    ]
    sha_lines: list[str] = []
    for name in artifact_names:
        content = (out_dir / name).read_text(encoding="utf-8")
        digest = _sha256_hex(content)
        sha_lines.append(f"{digest}  {name}")
    sha_lines.append("")  # trailing newline
    _atomic_write_text(out_dir / "sha256sums.txt", "\n".join(sha_lines))

    return out_dir
