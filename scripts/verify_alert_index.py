#!/usr/bin/env python3
"""Validate ALERT_INDEX.md against alert_rules.yml (OBS-7).

Checks:
1. Coverage: every alert in YAML is in the index, and vice versa
2. Severity bucket: each alert is in the correct severity section
3. Counts: section heading counts match actual row counts
4. Critical/page dashboard_uid: not empty/dash for required severities
5. Component/category: match YAML values

Usage:
  python -m scripts.verify_alert_index \\
      docs/runbooks/ALERT_INDEX.md monitoring/alert_rules.yml

Exit codes:
  0 - All checks passed
  1 - Validation errors found
  2 - File not found or parse error
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

import yaml


class AlertInfo(NamedTuple):
    """Alert metadata from either YAML or index."""

    name: str
    severity: str
    component: str
    category: str
    dashboard_uid: str  # "" if absent


# ---------------------------------------------------------------------------
# Parse alert_rules.yml
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = ("critical", "page", "warning", "ticket", "info")


def parse_yaml(path: Path) -> list[AlertInfo]:
    """Extract alert metadata from alert_rules.yml."""
    with path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)

    alerts: list[AlertInfo] = []
    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert", "")
            if not name:
                continue
            labels = rule.get("labels") or {}
            annotations = rule.get("annotations") or {}
            alerts.append(
                AlertInfo(
                    name=name,
                    severity=str(labels.get("severity", "")),
                    component=str(labels.get("component", "")),
                    category=str(labels.get("category", "")),
                    dashboard_uid=str(annotations.get("dashboard_uid", "")),
                )
            )
    return alerts


# ---------------------------------------------------------------------------
# Parse ALERT_INDEX.md
# ---------------------------------------------------------------------------

# Match section headers like "### Critical (9 alerts)"
_SECTION_RE = re.compile(
    r"^###\s+(Critical|Page|Warning|Ticket|Info)\s+\((\d+)\s+alerts?\)", re.IGNORECASE
)

# Match table rows: | `AlertName` | component | category | `uid` or — | ...
_ROW_RE = re.compile(
    r"^\|\s*`(\w+)`\s*\|"  # alert name
    r"\s*(\w+)\s*\|"  # component
    r"\s*(\w+)\s*\|"  # category
    r"\s*([^|]+?)\s*\|",  # dashboard_uid (may be `uid`, —, or empty)
)

_UID_EXTRACT = re.compile(r"`([^`]+)`")


def parse_index(path: Path) -> tuple[list[AlertInfo], dict[str, int]]:
    """Parse ALERT_INDEX.md, return (alerts, heading_counts).

    heading_counts maps severity -> count from section heading.
    """
    text = path.read_text()
    lines = text.splitlines()

    alerts: list[AlertInfo] = []
    heading_counts: dict[str, int] = {}
    current_severity = ""

    for line in lines:
        # Check for section header
        m = _SECTION_RE.match(line)
        if m:
            current_severity = m.group(1).lower()
            heading_counts[current_severity] = int(m.group(2))
            continue

        # Check for table data row
        if not current_severity or not line.startswith("|"):
            continue
        rm = _ROW_RE.match(line)
        if not rm:
            continue

        alert_name = rm.group(1)
        component = rm.group(2)
        category = rm.group(3)
        uid_raw = rm.group(4).strip()

        # Extract UID from backticks or treat — as empty
        uid = ""
        uid_m = _UID_EXTRACT.search(uid_raw)
        if uid_m:
            uid = uid_m.group(1)

        alerts.append(
            AlertInfo(
                name=alert_name,
                severity=current_severity,
                component=component,
                category=category,
                dashboard_uid=uid,
            )
        )

    return alerts, heading_counts


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_DASHBOARD_REQUIRED_SEVERITIES = frozenset({"critical", "page"})


def validate(
    yaml_alerts: list[AlertInfo],
    index_alerts: list[AlertInfo],
    heading_counts: dict[str, int],
) -> list[str]:
    """Run all checks. Returns list of error strings (empty = valid)."""
    errors: list[str] = []

    yaml_by_name: dict[str, AlertInfo] = {a.name: a for a in yaml_alerts}
    index_by_name: dict[str, AlertInfo] = {a.name: a for a in index_alerts}

    yaml_names = set(yaml_by_name)
    index_names = set(index_by_name)

    # 1. Coverage
    missing = yaml_names - index_names
    extra = index_names - yaml_names
    if missing:
        errors.append(f"missing from index: {sorted(missing)}")
    if extra:
        errors.append(f"extra in index (not in YAML): {sorted(extra)}")

    # 2. Severity bucket + 5. component/category
    for name in yaml_names & index_names:
        ya = yaml_by_name[name]
        ia = index_by_name[name]
        if ya.severity != ia.severity:
            errors.append(f"{name}: severity mismatch — YAML={ya.severity}, index={ia.severity}")
        if ya.component != ia.component:
            errors.append(f"{name}: component mismatch — YAML={ya.component}, index={ia.component}")
        if ya.category != ia.category:
            errors.append(f"{name}: category mismatch — YAML={ya.category}, index={ia.category}")

    # 3. Heading counts vs actual rows
    actual_counts: dict[str, int] = {}
    for a in index_alerts:
        actual_counts[a.severity] = actual_counts.get(a.severity, 0) + 1

    for sev in _SEVERITY_ORDER:
        heading = heading_counts.get(sev)
        actual = actual_counts.get(sev, 0)
        if heading is not None and heading != actual:
            errors.append(
                f"heading count mismatch for {sev}: heading says {heading}, actual rows = {actual}"
            )

    # 4. Critical/page must have dashboard_uid in index
    for a in index_alerts:
        if a.severity in _DASHBOARD_REQUIRED_SEVERITIES and not a.dashboard_uid:
            errors.append(
                f"{a.name}: severity={a.severity} requires dashboard_uid in index (got —)"
            )

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code."""
    parser = argparse.ArgumentParser(description="Validate ALERT_INDEX.md against alert_rules.yml")
    parser.add_argument("index_path", type=Path, help="Path to ALERT_INDEX.md")
    parser.add_argument("rules_path", type=Path, help="Path to alert_rules.yml")
    args = parser.parse_args(argv)

    for p in (args.index_path, args.rules_path):
        if not p.exists():
            print(f"ERROR: File not found: {p}", file=sys.stderr)
            return 2

    try:
        yaml_alerts = parse_yaml(args.rules_path)
    except (yaml.YAMLError, ValueError) as e:
        print(f"ERROR: Failed to parse {args.rules_path}: {e}", file=sys.stderr)
        return 2

    try:
        index_alerts, heading_counts = parse_index(args.index_path)
    except Exception as e:
        print(f"ERROR: Failed to parse {args.index_path}: {e}", file=sys.stderr)
        return 2

    errors = validate(yaml_alerts, index_alerts, heading_counts)
    if errors:
        print(f"FAIL: {len(errors)} error(s) found:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(index_alerts)} alerts validated against "
        f"{len(yaml_alerts)} YAML rules — all checks passed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
