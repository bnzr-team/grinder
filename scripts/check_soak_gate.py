#!/usr/bin/env python3
"""Deterministic soak gate for CI.

Gates ONLY deterministic metrics that don't vary with CI runner performance:
- Digest stability (all runs produce identical output)
- Error count
- Fill rate range
- Events dropped

Latency and memory are NOT gated (CI runners have variable performance).
Use `python -m scripts.check_soak_thresholds` for full threshold validation in controlled environments.

Usage:
    python -m scripts.check_soak_gate \
        --report artifacts/soak_fixtures.json \
        --thresholds monitoring/soak_thresholds.yml \
        --mode baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON file."""
    with path.open(encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


def load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML file."""
    with path.open(encoding="utf-8") as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def _get_fill_rate_bounds(fill_rate_config: dict[str, float] | float) -> tuple[float, float]:
    """Extract min/max bounds from fill_rate config.

    Supports two formats:
    - dict: {"min": 0.4, "max": 1.0}
    - scalar: 1.0 (treated as max, min defaults to 0.0)
    """
    if isinstance(fill_rate_config, dict):
        return fill_rate_config.get("min", 0.0), fill_rate_config.get("max", 1.0)
    return 0.0, float(fill_rate_config)


def check_deterministic_gates(
    report: dict[str, Any],
    thresholds: dict[str, Any],
    mode: str,
) -> list[str]:
    """Check deterministic gates and return list of failures.

    Gates:
    - all_digests_stable: must be True
    - errors_total: must be <= threshold
    - fill_rate: must be in [min, max]
    - events_dropped: must be <= threshold (if present)

    Returns:
        List of failure messages (empty if all pass).
    """
    failures: list[str] = []
    mode_thresholds = thresholds.get(mode, {})

    # Gate 1: Digest stability (determinism) - always required
    if not report.get("all_digests_stable", False):
        failures.append("Digest instability detected!")
        for r in report.get("results", []):
            if not r.get("digest_stable", True):
                failures.append(f"  - {r['fixture_path']}: UNSTABLE")

    # Gate 2: Error count
    errors_threshold = mode_thresholds.get("errors_total", 0)
    errors_actual = report.get("errors_total", 0)
    if errors_actual > errors_threshold:
        failures.append(f"errors_total: {errors_actual} > {errors_threshold} (threshold)")
        for r in report.get("results", []):
            if r.get("errors"):
                failures.append(f"  - {r['fixture_path']}: {r['errors']}")

    # Gate 3: Fill rate range
    fill_min, fill_max = _get_fill_rate_bounds(mode_thresholds.get("fill_rate", {}))
    fill_actual = report.get("fill_rate", 0.0)
    if fill_actual < fill_min:
        failures.append(f"fill_rate: {fill_actual:.4f} < {fill_min} (min threshold)")
    if fill_actual > fill_max:
        failures.append(f"fill_rate: {fill_actual:.4f} > {fill_max} (max threshold)")

    # Gate 4: Events dropped (if threshold defined)
    if "events_dropped" in mode_thresholds:
        dropped_threshold = mode_thresholds["events_dropped"]
        dropped_actual = report.get("events_dropped", 0)
        if dropped_actual > dropped_threshold:
            failures.append(f"events_dropped: {dropped_actual} > {dropped_threshold} (threshold)")

    return failures


def _print_gate_results(
    report: dict[str, Any],
    mode_thresholds: dict[str, Any],
) -> None:
    """Print PASS/FAIL status for each gate."""
    # Digest stability
    stable = report.get("all_digests_stable", False)
    print("PASS: All digests stable" if stable else "FAIL: Digest instability detected")

    # Errors
    errors_threshold = mode_thresholds.get("errors_total", 0)
    errors_actual = report.get("errors_total", 0)
    status = "PASS" if errors_actual <= errors_threshold else "FAIL"
    print(f"{status}: errors_total = {errors_actual} (threshold: {errors_threshold})")

    # Fill rate
    fill_min, fill_max = _get_fill_rate_bounds(mode_thresholds.get("fill_rate", {}))
    fill_actual = report.get("fill_rate", 0.0)
    in_range = fill_min <= fill_actual <= fill_max
    status = "PASS" if in_range else "FAIL"
    print(f"{status}: fill_rate = {fill_actual:.4f} (range: [{fill_min}, {fill_max}])")

    # Events dropped (if configured)
    if "events_dropped" in mode_thresholds:
        dropped_threshold = mode_thresholds["events_dropped"]
        dropped_actual = report.get("events_dropped", 0)
        status = "PASS" if dropped_actual <= dropped_threshold else "FAIL"
        print(f"{status}: events_dropped = {dropped_actual} (threshold: {dropped_threshold})")


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Check deterministic soak gates")
    parser.add_argument("--report", type=Path, required=True, help="Path to soak report JSON")
    parser.add_argument("--thresholds", type=Path, required=True, help="Path to thresholds YAML")
    parser.add_argument(
        "--mode",
        choices=["baseline", "overload"],
        default="baseline",
        help="Threshold mode (default: baseline)",
    )
    args = parser.parse_args()

    # Validate inputs
    if not args.report.exists():
        print(f"ERROR: Report not found: {args.report}")
        sys.exit(1)
    if not args.thresholds.exists():
        print(f"ERROR: Thresholds not found: {args.thresholds}")
        sys.exit(1)

    # Load files and check gates
    report = load_json(args.report)
    thresholds = load_yaml(args.thresholds)
    failures = check_deterministic_gates(report, thresholds, args.mode)

    # Print results
    print(f"Soak Gate Check (mode={args.mode})")
    print("=" * 40)
    _print_gate_results(report, thresholds.get(args.mode, {}))

    # Print informational metrics
    print("-" * 40)
    print("Informational metrics:")
    print(f"  latency_p99 = {report.get('decision_latency_p99_ms', 0):.2f}ms")
    print(f"  rss_mb_max = {report.get('rss_mb_max', 0):.0f}MB")
    print("-" * 40)

    # Exit with appropriate code
    if failures:
        print(f"\nFAILED: {len(failures)} gate(s) failed")
        sys.exit(1)
    print("\nAll deterministic gates passed.")


if __name__ == "__main__":
    main()
