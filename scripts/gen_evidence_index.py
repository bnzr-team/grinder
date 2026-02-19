#!/usr/bin/env python3
"""Generate docs/runbooks/00_EVIDENCE_INDEX.json from evidence registry.

This script is the SSOT for the machine-readable evidence index.
The companion markdown (00_EVIDENCE_INDEX.md) is the human-readable version.

Usage:
    python3 scripts/gen_evidence_index.py

Exit codes:
    0  JSON written successfully
    1  Validation error (duplicate id/mode, missing keys, broken doc refs)
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_PATH = PROJECT_ROOT / "docs" / "runbooks" / "00_EVIDENCE_INDEX.md"
JSON_PATH = PROJECT_ROOT / "docs" / "runbooks" / "00_EVIDENCE_INDEX.json"

# =========================================================================
# Evidence registry (SSOT)
# =========================================================================

ENTRIES: list[dict[str, Any]] = [
    {
        "id": "fill_wiring_local",
        "title": "Fill wiring (local)",
        "run_via": [
            "bash scripts/ops_fill_triage.sh local",
            "bash scripts/smoke_fill_ingest.sh",
        ],
        "mode": "local",
        "evidence_dir_pattern": None,
        "required_files": [],
        "optional_files": [],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "local-smoke-smoke_fill_ingestsh",
            },
        ],
    },
    {
        "id": "staging_enablement",
        "title": "Staging enablement",
        "run_via": [
            "bash scripts/ops_fill_triage.sh staging",
            "bash scripts/smoke_fill_ingest_staging.sh",
        ],
        "mode": "staging",
        "evidence_dir_pattern": ".artifacts/fill_ingest_staging/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "gate_a_metrics.txt",
        ],
        "optional_files": [
            "gate_b_metrics.txt",
            "gate_c_metrics.txt",
            "cursor_after_run1.json",
            "cursor_before_restart.json",
            "cursor_after_run2.json",
        ],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "staging-smoke-smoke_fill_ingest_stagingsh",
            },
        ],
    },
    {
        "id": "alert_inputs_non_monotonic",
        "title": "Alert inputs (non-monotonic rejection)",
        "run_via": [
            "bash scripts/ops_fill_triage.sh fire-drill",
            "bash scripts/fire_drill_fill_alerts.sh",
        ],
        "mode": "fire-drill",
        "evidence_dir_pattern": ".artifacts/fill_alert_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
            "drill_a_metrics.txt",
            "drill_a_log.txt",
        ],
        "optional_files": [
            "cursor_before_drill_a.json",
            "cursor_after_drill_a.json",
            "cursor_drill_a.json",
        ],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "fire-drill-fire_drill_fill_alertssh",
            },
        ],
    },
    {
        "id": "alert_inputs_cursor_stuck",
        "title": "Alert inputs (cursor stuck)",
        "run_via": [
            "bash scripts/ops_fill_triage.sh fire-drill",
            "bash scripts/fire_drill_fill_alerts.sh",
        ],
        "mode": "fire-drill",
        "evidence_dir_pattern": ".artifacts/fill_alert_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
            "drill_b_metrics_1.txt",
            "drill_b_metrics_2.txt",
            "drill_b_log.txt",
        ],
        "optional_files": [
            "cursor_drill_b.json",
        ],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "fire-drill-fire_drill_fill_alertssh",
            },
        ],
    },
    {
        "id": "killswitch_enforcement",
        "title": "Kill-switch + enforcement",
        "run_via": [
            "bash scripts/ops_risk_triage.sh killswitch-drawdown",
            "bash scripts/fire_drill_risk_killswitch_drawdown.sh",
        ],
        "mode": "killswitch-drawdown",
        "evidence_dir_pattern": ".artifacts/risk_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
            "drill_a_metrics.txt",
            "drill_a_log.txt",
        ],
        "optional_files": [],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "risk-fire-drill-fire_drill_risk_killswitch_drawdownsh",
            },
        ],
    },
    {
        "id": "drawdown_guard",
        "title": "Drawdown guard",
        "run_via": [
            "bash scripts/ops_risk_triage.sh killswitch-drawdown",
            "bash scripts/fire_drill_risk_killswitch_drawdown.sh",
        ],
        "mode": "killswitch-drawdown",
        "evidence_dir_pattern": ".artifacts/risk_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
            "drill_b_metrics.txt",
            "drill_b_log.txt",
        ],
        "optional_files": [],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "risk-fire-drill-fire_drill_risk_killswitch_drawdownsh",
            },
        ],
    },
    {
        "id": "budget_per_run_cap",
        "title": "Budget per-run cap",
        "run_via": [
            "bash scripts/ops_risk_triage.sh budget-limits",
            "bash scripts/fire_drill_reconcile_budget_limits.sh",
        ],
        "mode": "budget-limits",
        "evidence_dir_pattern": ".artifacts/budget_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
            "drill_a_metrics.txt",
            "drill_a_log.txt",
            "drill_a_state.json",
        ],
        "optional_files": [],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "budget-fire-drill-fire_drill_reconcile_budget_limitssh",
            },
        ],
    },
    {
        "id": "budget_per_day_cap",
        "title": "Budget per-day cap",
        "run_via": [
            "bash scripts/ops_risk_triage.sh budget-limits",
            "bash scripts/fire_drill_reconcile_budget_limits.sh",
        ],
        "mode": "budget-limits",
        "evidence_dir_pattern": ".artifacts/budget_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
            "drill_b_metrics.txt",
            "drill_b_log.txt",
            "drill_b_state.json",
        ],
        "optional_files": [],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "budget-fire-drill-fire_drill_reconcile_budget_limitssh",
            },
        ],
    },
    {
        "id": "execution_intent_gates",
        "title": "Execution intent gates",
        "run_via": [
            "bash scripts/ops_exec_triage.sh exec-fire-drill",
            "bash scripts/fire_drill_execution_intents.sh",
        ],
        "mode": "exec-fire-drill",
        "evidence_dir_pattern": ".artifacts/execution_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
        ],
        "optional_files": [
            "drill_a_metrics.txt",
            "drill_a_log.txt",
            "drill_b_metrics.txt",
            "drill_b_log.txt",
            "drill_c_metrics.txt",
            "drill_c_log.txt",
            "drill_d_metrics.txt",
            "drill_d_log.txt",
        ],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "execution-fire-drill-fire_drill_execution_intentssh",
            },
        ],
    },
    {
        "id": "connector_market_data",
        "title": "Market data connector",
        "run_via": [
            "bash scripts/ops_fill_triage.sh connector-market-data",
            "bash scripts/fire_drill_connector_market_data.sh",
        ],
        "mode": "connector-market-data",
        "evidence_dir_pattern": ".artifacts/connector_market_data_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
        ],
        "optional_files": [
            "drill_a_log.txt",
            "drill_b_log.txt",
            "drill_b_metrics.txt",
            "drill_c_log.txt",
            "drill_d_log.txt",
            "drill_d_metrics.txt",
            "drill_e_log.txt",
            "drill_e_metrics.txt",
        ],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "market-data-connector-fire-drill",
            },
        ],
    },
    {
        "id": "connector_exchange_port",
        "title": "Exchange port boundary",
        "run_via": [
            "bash scripts/ops_fill_triage.sh connector-exchange-port",
            "bash scripts/fire_drill_connector_exchange_port.sh",
        ],
        "mode": "connector-exchange-port",
        "evidence_dir_pattern": ".artifacts/connector_exchange_port_fire_drill/<YYYYMMDDTHHMMSS>",
        "required_files": [
            "summary.txt",
            "sha256sums.txt",
        ],
        "optional_files": [
            "drill_a_log.txt",
            "drill_b_log.txt",
            "drill_c_log.txt",
            "drill_d_log.txt",
            "drill_e_log.txt",
            "drill_e_metrics.txt",
            "drill_f_log.txt",
            "drill_f_metrics.txt",
        ],
        "doc_refs": [
            {
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
                "anchor": "exchange-port-boundary-fire-drill",
            },
        ],
    },
]

# =========================================================================
# Required keys per entry
# =========================================================================

REQUIRED_KEYS = {
    "id",
    "title",
    "run_via",
    "mode",
    "evidence_dir_pattern",
    "required_files",
    "optional_files",
    "doc_refs",
}


def _git_sha() -> str:
    """Return current HEAD SHA, or 'unknown' if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def validate(entries: list[dict[str, Any]]) -> list[str]:
    """Validate entries. Returns list of error strings (empty = ok)."""
    errors: list[str] = []

    seen_ids: set[str] = set()
    for i, entry in enumerate(entries):
        # Check required keys.
        missing = REQUIRED_KEYS - set(entry.keys())
        if missing:
            errors.append(f"entry[{i}]: missing keys: {sorted(missing)}")

        eid = entry.get("id", f"<missing-id-{i}>")

        # Duplicate id check.
        if eid in seen_ids:
            errors.append(f"entry[{i}]: duplicate id '{eid}'")
        seen_ids.add(eid)

        # run_via must be non-empty.
        if not entry.get("run_via"):
            errors.append(f"entry[{i}] ({eid}): run_via must not be empty")

        # doc_refs sanity: check referenced files exist.
        for ref in entry.get("doc_refs", []):
            ref_path = PROJECT_ROOT / ref["path"]
            if not ref_path.exists():
                errors.append(f"entry[{i}] ({eid}): doc_ref path does not exist: {ref['path']}")

    # Check companion markdown exists.
    if not MARKDOWN_PATH.exists():
        errors.append(f"companion markdown not found: {MARKDOWN_PATH}")

    return errors


def generate() -> dict[str, Any]:
    """Build the full JSON document."""
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [
            {
                "type": "markdown",
                "path": "docs/runbooks/00_EVIDENCE_INDEX.md",
            },
            {
                "type": "git",
                "sha": _git_sha(),
            },
        ],
        "entries": ENTRIES,
    }


def main() -> int:
    errors = validate(ENTRIES)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    doc = generate()
    json_str = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    JSON_PATH.write_text(json_str, encoding="utf-8")
    print(f"Written {JSON_PATH} ({len(ENTRIES)} entries, schema v1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
