#!/usr/bin/env python3
"""Validate Prometheus alert rules YAML (Launch-04).

Checks:
1. Valid YAML structure with ``groups[].rules[]``
2. Unique alert names across all groups
3. No ``symbol=`` in expr, labels, or annotations (forbidden label)
4. Non-empty ``expr`` on every rule

Usage:
  python -m scripts.verify_alert_rules monitoring/alert_rules.yml

Exit codes:
  0 - All checks passed
  1 - Validation errors found
  2 - File not found or invalid YAML
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# Pattern matching symbol= in PromQL expressions and label values.
# Catches: symbol=, symbol!=, symbol=~, symbol!~
SYMBOL_PATTERN = re.compile(r"\bsymbol\s*[!=~]")


def load_rules(path: Path) -> dict[str, Any]:
    """Load and parse alert rules YAML.

    Raises:
        FileNotFoundError: If path does not exist.
        yaml.YAMLError: If YAML is invalid.
    """
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"Expected YAML dict, got {type(data).__name__}"
        raise ValueError(msg)
    return data


def validate(data: dict[str, Any]) -> list[str]:
    """Validate alert rules structure and constraints.

    Returns list of error strings (empty = valid).
    """
    errors: list[str] = []

    groups = data.get("groups")
    if not isinstance(groups, list):
        errors.append("Missing or invalid 'groups' key (expected list)")
        return errors

    seen_names: dict[str, str] = {}  # alert_name -> group_name

    for group in groups:
        group_name = group.get("name", "<unnamed>")
        rules = group.get("rules")
        if not isinstance(rules, list):
            errors.append(f"Group '{group_name}': missing or invalid 'rules' (expected list)")
            continue

        for i, rule in enumerate(rules):
            alert_name = rule.get("alert", "")
            rule_id = f"Group '{group_name}', rule {i} ('{alert_name}')"

            # Unique alert name
            if alert_name:
                if alert_name in seen_names:
                    errors.append(
                        f"{rule_id}: duplicate alert name "
                        f"(already in group '{seen_names[alert_name]}')"
                    )
                seen_names[alert_name] = group_name

            # Non-empty expr
            expr = rule.get("expr", "")
            if not str(expr).strip():
                errors.append(f"{rule_id}: empty 'expr'")

            # symbol= check across all string fields
            _check_symbol(rule, rule_id, errors)

    return errors


def _check_symbol(obj: Any, context: str, errors: list[str], path: str = "") -> None:
    """Recursively check for symbol= in all string values."""
    if isinstance(obj, str):
        if SYMBOL_PATTERN.search(obj):
            errors.append(f"{context}: forbidden 'symbol=' found in {path or 'value'}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_symbol(v, context, errors, path=f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_symbol(v, context, errors, path=f"{path}[{i}]" if path else f"[{i}]")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code."""
    parser = argparse.ArgumentParser(description="Validate Prometheus alert rules")
    parser.add_argument("path", type=Path, help="Path to alert_rules.yml")
    args = parser.parse_args(argv)

    path: Path = args.path
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        return 2

    try:
        data = load_rules(path)
    except (yaml.YAMLError, ValueError) as e:
        print(f"ERROR: Invalid YAML: {e}", file=sys.stderr)
        return 2

    errors = validate(data)
    if errors:
        print(f"FAIL: {len(errors)} error(s) found:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # Count rules for summary
    rule_count = sum(len(g.get("rules", [])) for g in data.get("groups", []))
    group_count = len(data.get("groups", []))
    print(f"OK: {rule_count} rules in {group_count} groups — all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
