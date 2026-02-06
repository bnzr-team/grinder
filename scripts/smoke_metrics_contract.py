#!/usr/bin/env python3
"""LC-16: Observability Metrics Contract Smoke Test.

Validates that /metrics output contains all required patterns from
src/grinder/observability/live_contract.py:REQUIRED_METRICS_PATTERNS
and does not contain any FORBIDDEN_METRIC_LABELS.

Usage:
    # Against live service
    python scripts/smoke_metrics_contract.py --url http://localhost:9090/metrics

    # Against file (for testing)
    python scripts/smoke_metrics_contract.py --file /tmp/metrics.txt

    # With verbose output
    python scripts/smoke_metrics_contract.py --url http://localhost:9090/metrics -v

Exit codes:
    0 - All patterns present, no forbidden labels
    1 - Missing patterns or forbidden labels found
    2 - Connection/file error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from grinder.observability.live_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)

EXIT_SUCCESS = 0
EXIT_VALIDATION_FAILED = 1
EXIT_CONNECTION_ERROR = 2


def fetch_metrics(url: str, timeout_s: float = 10.0) -> str:
    """Fetch metrics from URL."""
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.TimeoutException as e:
        raise ConnectionError(f"Timeout fetching {url}: {e}") from e
    except httpx.ConnectError as e:
        raise ConnectionError(f"Connection error fetching {url}: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ConnectionError(f"HTTP error fetching {url}: {e}") from e


def validate_patterns(metrics_text: str, verbose: bool = False) -> tuple[list[str], list[str]]:
    """Validate metrics text against required patterns.

    Returns:
        Tuple of (missing_patterns, found_patterns)
    """
    missing = []
    found = []

    for pattern in REQUIRED_METRICS_PATTERNS:
        if pattern in metrics_text:
            found.append(pattern)
            if verbose:
                print(f"  ✓ {pattern[:60]}...")
        else:
            missing.append(pattern)
            if verbose:
                print(f"  ✗ MISSING: {pattern}")

    return missing, found


def check_forbidden_labels(metrics_text: str, verbose: bool = False) -> list[str]:
    """Check for forbidden high-cardinality labels.

    Returns:
        List of forbidden labels found
    """
    found_forbidden = []

    for label in FORBIDDEN_METRIC_LABELS:
        if label in metrics_text:
            found_forbidden.append(label)
            if verbose:
                print(f"  ✗ FORBIDDEN: {label}")

    return found_forbidden


def main() -> int:  # noqa: PLR0915
    """Run metrics contract validation."""
    parser = argparse.ArgumentParser(
        description="Validate /metrics against REQUIRED_METRICS_PATTERNS"
    )
    parser.add_argument(
        "--url",
        type=str,
        help="URL to fetch metrics from (e.g., http://localhost:9090/metrics)",
    )
    parser.add_argument(
        "--file",
        type=str,
        help="File to read metrics from (for testing)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output for each pattern",
    )
    args = parser.parse_args()

    if not args.url and not args.file:
        print("ERROR: Must specify --url or --file")
        return EXIT_CONNECTION_ERROR

    # Fetch or read metrics
    print("=" * 60)
    print("  LC-16: METRICS CONTRACT VALIDATION")
    print("=" * 60)

    try:
        if args.url:
            print(f"\nFetching metrics from: {args.url}")
            metrics_text = fetch_metrics(args.url)
        else:
            print(f"\nReading metrics from: {args.file}")
            metrics_text = Path(args.file).read_text()
    except ConnectionError as e:
        print(f"\nERROR: {e}")
        return EXIT_CONNECTION_ERROR
    except FileNotFoundError as e:
        print(f"\nERROR: File not found: {e}")
        return EXIT_CONNECTION_ERROR

    metrics_lines = len(metrics_text.strip().split("\n"))
    print(f"Metrics size: {len(metrics_text)} bytes, {metrics_lines} lines")

    # Validate required patterns
    print(f"\n--- Required Patterns ({len(REQUIRED_METRICS_PATTERNS)} total) ---")
    missing, found = validate_patterns(metrics_text, verbose=args.verbose)

    # Check forbidden labels
    print(f"\n--- Forbidden Labels ({len(FORBIDDEN_METRIC_LABELS)} checked) ---")
    forbidden_found = check_forbidden_labels(metrics_text, verbose=args.verbose)

    # Summary
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)

    print("\n  Required patterns:")
    print(f"    Total:   {len(REQUIRED_METRICS_PATTERNS)}")
    print(f"    Found:   {len(found)}")
    print(f"    Missing: {len(missing)}")

    print("\n  Forbidden labels:")
    print(f"    Checked: {len(FORBIDDEN_METRIC_LABELS)}")
    print(f"    Found:   {len(forbidden_found)}")

    # Detailed missing patterns
    if missing:
        print("\n  MISSING PATTERNS:")
        for pattern in missing:
            print(f"    - {pattern}")

    # Detailed forbidden labels
    if forbidden_found:
        print("\n  FORBIDDEN LABELS FOUND:")
        for label in forbidden_found:
            print(f"    - {label}")

    # Final verdict
    print("\n" + "=" * 60)
    if not missing and not forbidden_found:
        print("  METRICS CONTRACT VALIDATED ✓")
        print("=" * 60)
        return EXIT_SUCCESS
    else:
        print("  METRICS CONTRACT FAILED ✗")
        print("=" * 60)
        return EXIT_VALIDATION_FAILED


if __name__ == "__main__":
    sys.exit(main())
