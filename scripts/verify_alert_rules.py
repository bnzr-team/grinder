#!/usr/bin/env python3
"""Validate Prometheus alert rules YAML (Launch-05).

Checks:
1. Valid YAML structure with ``groups[].rules[]``
2. Unique alert names across all groups
3. No forbidden labels in expr (``symbol=``, ``order_id=``, ``key=``, ``client_id=``)
4. Non-empty ``expr`` on every rule
5. ``op=`` values in PromQL expressions match ops taxonomy allowlist

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

# Forbidden labels — high-cardinality or sensitive data that must never appear
# in alert expressions. Pattern matches: label=, label!=, label=~, label!~
_FORBIDDEN_LABELS: tuple[str, ...] = ("symbol", "order_id", "key", "client_id")
FORBIDDEN_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(lbl) for lbl in _FORBIDDEN_LABELS) + r")\s*[!=~]"
)

# Ops taxonomy allowlist — SSOT from src/grinder/net/retry_policy.py
_OPS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "place_order",
        "cancel_order",
        "cancel_all",
        "get_open_orders",
        "get_positions",
        "get_account",
        "exchange_info",
        "ping_time",
        "get_user_trades",
    }
)

# Pattern to extract op= literal values from PromQL expressions.
# Matches: op="value", op=~"value|value2", op!="value"
_OP_VALUE_PATTERN = re.compile(r'\bop\s*[!=~]+\s*"([^"]*)"')


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

            # Forbidden label check across all string fields
            _check_forbidden_labels(rule, rule_id, errors)

            # op= value allowlist check on expr
            _check_op_allowlist(str(expr), rule_id, errors)

    return errors


def _check_forbidden_labels(obj: Any, context: str, errors: list[str], path: str = "") -> None:
    """Recursively check for forbidden labels in all string values."""
    if isinstance(obj, str):
        match = FORBIDDEN_PATTERN.search(obj)
        if match:
            errors.append(
                f"{context}: forbidden label '{match.group(1)}=' found in {path or 'value'}"
            )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_forbidden_labels(v, context, errors, path=f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_forbidden_labels(v, context, errors, path=f"{path}[{i}]" if path else f"[{i}]")


def _check_op_allowlist(expr: str, context: str, errors: list[str]) -> None:
    """Check that op= values in PromQL expressions are from the ops taxonomy."""
    for match in _OP_VALUE_PATTERN.finditer(expr):
        raw_value = match.group(1)
        # Split on | for regex alternation (op=~"cancel_order|place_order")
        ops = [op.strip() for op in raw_value.split("|") if op.strip()]
        for op in ops:
            if op not in _OPS_ALLOWLIST:
                errors.append(f"{context}: unknown op '{op}' in expr (not in ops taxonomy)")


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
