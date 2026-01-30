#!/usr/bin/env python3
"""
Validate soak test results against thresholds.

Usage:
    python scripts/check_soak_thresholds.py \
        --baseline artifacts/soak_baseline.json \
        --overload artifacts/soak_overload.json \
        --thresholds monitoring/soak_thresholds.yml
"""

import argparse
import json
import sys
from pathlib import Path

import yaml


def load_json(path: Path) -> dict:
    """Load JSON file."""
    with open(path) as f:
        return json.load(f)


def load_yaml(path: Path) -> dict:
    """Load YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


def check_thresholds(results: dict, thresholds: dict, mode: str) -> list[str]:
    """Check results against thresholds. Returns list of violations."""
    violations = []
    mode_thresholds = thresholds.get(mode, {})

    for metric, threshold in mode_thresholds.items():
        if metric not in results:
            continue

        value = results[metric]

        if isinstance(threshold, dict):
            # Threshold with min/max
            if "max" in threshold and value > threshold["max"]:
                violations.append(
                    f"{mode}.{metric}: {value} > max({threshold['max']})"
                )
            if "min" in threshold and value < threshold["min"]:
                violations.append(
                    f"{mode}.{metric}: {value} < min({threshold['min']})"
                )
        else:
            # Simple max threshold
            if value > threshold:
                violations.append(
                    f"{mode}.{metric}: {value} > {threshold}"
                )

    return violations


def main():
    parser = argparse.ArgumentParser(description="Check soak test thresholds")
    parser.add_argument("--baseline", type=Path, required=True, help="Baseline results JSON")
    parser.add_argument("--overload", type=Path, required=True, help="Overload results JSON")
    parser.add_argument("--thresholds", type=Path, required=True, help="Thresholds YAML")
    args = parser.parse_args()

    # Load files
    baseline = load_json(args.baseline)
    overload = load_json(args.overload)
    thresholds = load_yaml(args.thresholds)

    all_violations = []

    # Check baseline
    print("Checking baseline thresholds...")
    violations = check_thresholds(baseline, thresholds, "baseline")
    if violations:
        print(f"  FAIL: {len(violations)} violation(s)")
        all_violations.extend(violations)
    else:
        print("  PASS")

    # Check overload
    print("Checking overload thresholds...")
    violations = check_thresholds(overload, thresholds, "overload")
    if violations:
        print(f"  FAIL: {len(violations)} violation(s)")
        all_violations.extend(violations)
    else:
        print("  PASS")

    # Report
    if all_violations:
        print("\nThreshold violations:")
        for v in all_violations:
            print(f"  - {v}")
        sys.exit(1)
    else:
        print("\nAll thresholds passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
