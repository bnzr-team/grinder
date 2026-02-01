#!/usr/bin/env python3
"""Determinism Gate v1 — verify all fixtures and backtest are deterministic.

This script is a CI gate that catches silent drift across:
- Replay digests (deterministic replay)
- Paper digests (paper v1 output)
- Backtest report digest (aggregate deterministic report)

Usage:
    python -m scripts.verify_determinism_suite
    python -m scripts.verify_determinism_suite --quiet

Exit codes:
    0 - All checks pass
    1 - Any mismatch/drift/missing expected fields
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grinder.paper import PaperEngine
from grinder.replay import ReplayEngine

# Fixture discovery path
FIXTURES_DIR = Path("tests/fixtures")


@dataclass
class FixtureCheck:
    """Result of checking a single fixture."""

    name: str
    replay_digest_1: str
    replay_digest_2: str
    replay_match: bool
    replay_expected: str
    replay_expected_match: bool
    paper_digest_1: str
    paper_digest_2: str
    paper_match: bool
    paper_expected: str
    paper_expected_match: bool
    errors: list[str]

    @property
    def passed(self) -> bool:
        """Check if all assertions passed for this fixture."""
        return (
            self.replay_match
            and self.replay_expected_match
            and self.paper_match
            and self.paper_expected_match
            and not self.errors
        )


@dataclass
class BacktestCheck:
    """Result of checking backtest determinism."""

    report_digest_1: str
    report_digest_2: str
    match: bool
    all_fixtures_passed: bool
    errors: list[str]

    @property
    def passed(self) -> bool:
        """Check if backtest determinism passed."""
        return self.match and self.all_fixtures_passed and not self.errors


def discover_fixtures() -> list[Path]:
    """Discover fixtures by looking for config.json files.

    Returns fixtures sorted lexicographically by directory name.
    """
    fixtures = []
    if FIXTURES_DIR.exists():
        for child in sorted(FIXTURES_DIR.iterdir()):
            if child.is_dir() and (child / "config.json").exists():
                fixtures.append(child)
    return fixtures


def load_config(fixture_path: Path) -> dict[str, Any]:
    """Load fixture config.json."""
    config_path = fixture_path / "config.json"
    if config_path.exists():
        with config_path.open() as f:
            result: dict[str, Any] = json.load(f)
            return result
    return {}


def run_replay(fixture_path: Path) -> str:
    """Run replay and return digest."""
    engine = ReplayEngine()
    result = engine.run(fixture_path)
    return result.digest


def run_paper(fixture_path: Path, controller_enabled: bool = False) -> str:
    """Run paper trading and return digest."""
    engine = PaperEngine(controller_enabled=controller_enabled)
    result = engine.run(fixture_path)
    return result.digest


def run_backtest() -> tuple[str, bool]:
    """Run backtest and return (report_digest, all_fixtures_passed)."""
    # Late import to avoid circular dependency with scripts module
    from scripts.run_backtest import run_backtest as _run_backtest  # noqa: PLC0415

    report = _run_backtest()
    return report.report_digest, report.all_digests_match


def check_fixture(fixture_path: Path, verbose: bool = False) -> FixtureCheck:
    """Check a single fixture for determinism."""
    name = fixture_path.name
    config = load_config(fixture_path)
    controller_enabled = bool(config.get("controller_enabled", False))

    errors: list[str] = []

    # Get expected digests from config
    replay_expected = config.get("expected_digest", "")
    paper_expected = config.get("expected_paper_digest", "")

    if verbose:
        print(f"  Checking {name}...")

    # Run replay twice
    try:
        replay_1 = run_replay(fixture_path)
        replay_2 = run_replay(fixture_path)
    except Exception as e:
        errors.append(f"Replay error: {e}")
        replay_1 = replay_2 = ""

    replay_match = replay_1 == replay_2 and replay_1 != ""

    # Check replay against expected (if specified, otherwise skip)
    replay_expected_match = replay_1 == replay_expected if replay_expected else True

    # Run paper twice
    try:
        paper_1 = run_paper(fixture_path, controller_enabled)
        paper_2 = run_paper(fixture_path, controller_enabled)
    except Exception as e:
        errors.append(f"Paper error: {e}")
        paper_1 = paper_2 = ""

    paper_match = paper_1 == paper_2 and paper_1 != ""

    # Check paper against expected (if specified, otherwise skip)
    paper_expected_match = paper_1 == paper_expected if paper_expected else True

    return FixtureCheck(
        name=name,
        replay_digest_1=replay_1,
        replay_digest_2=replay_2,
        replay_match=replay_match,
        replay_expected=replay_expected,
        replay_expected_match=replay_expected_match,
        paper_digest_1=paper_1,
        paper_digest_2=paper_2,
        paper_match=paper_match,
        paper_expected=paper_expected,
        paper_expected_match=paper_expected_match,
        errors=errors,
    )


def check_backtest(verbose: bool = False) -> BacktestCheck:
    """Check backtest for determinism."""
    errors: list[str] = []

    if verbose:
        print("  Running backtest (run 1)...")

    try:
        digest_1, all_passed_1 = run_backtest()
    except Exception as e:
        errors.append(f"Backtest run 1 error: {e}")
        digest_1 = ""
        all_passed_1 = False

    if verbose:
        print("  Running backtest (run 2)...")

    try:
        digest_2, all_passed_2 = run_backtest()
    except Exception as e:
        errors.append(f"Backtest run 2 error: {e}")
        digest_2 = ""
        all_passed_2 = False

    match = digest_1 == digest_2 and digest_1 != ""
    all_passed = all_passed_1 and all_passed_2

    return BacktestCheck(
        report_digest_1=digest_1,
        report_digest_2=digest_2,
        match=match,
        all_fixtures_passed=all_passed,
        errors=errors,
    )


def print_summary(  # noqa: PLR0912
    fixture_checks: list[FixtureCheck],
    backtest_check: BacktestCheck,
    verbose: bool = False,
) -> None:
    """Print summary of all checks."""
    print("\n" + "=" * 60)
    print("DETERMINISM SUITE REPORT")
    print("=" * 60)

    # Fixture summary
    print("\n## Fixtures\n")
    print(f"{'Fixture':<30} {'Replay':<12} {'Paper':<12} {'Status':<8}")
    print("-" * 62)

    for check in fixture_checks:
        replay_status = "OK" if check.replay_match and check.replay_expected_match else "FAIL"
        paper_status = "OK" if check.paper_match and check.paper_expected_match else "FAIL"
        overall = "PASS" if check.passed else "FAIL"
        print(f"{check.name:<30} {replay_status:<12} {paper_status:<12} {overall:<8}")

        if verbose and not check.passed:
            if not check.replay_match:
                print(
                    f"  ! Replay non-deterministic: {check.replay_digest_1} vs {check.replay_digest_2}"
                )
            if not check.replay_expected_match and check.replay_expected:
                print(
                    f"  ! Replay expected mismatch: got {check.replay_digest_1}, expected {check.replay_expected}"
                )
            if not check.paper_match:
                print(
                    f"  ! Paper non-deterministic: {check.paper_digest_1} vs {check.paper_digest_2}"
                )
            if not check.paper_expected_match and check.paper_expected:
                print(
                    f"  ! Paper expected mismatch: got {check.paper_digest_1}, expected {check.paper_expected}"
                )
            for err in check.errors:
                print(f"  ! {err}")

    # Backtest summary
    print("\n## Backtest\n")
    backtest_status = "PASS" if backtest_check.passed else "FAIL"
    print(f"Report digest determinism: {'OK' if backtest_check.match else 'FAIL'}")
    print(f"All fixtures passed: {'OK' if backtest_check.all_fixtures_passed else 'FAIL'}")
    print(f"Overall: {backtest_status}")

    if verbose and not backtest_check.passed:
        if not backtest_check.match:
            print(
                f"  ! Backtest non-deterministic: {backtest_check.report_digest_1} vs {backtest_check.report_digest_2}"
            )
        for err in backtest_check.errors:
            print(f"  ! {err}")

    # Final verdict
    all_fixtures_passed = all(c.passed for c in fixture_checks)
    all_passed = all_fixtures_passed and backtest_check.passed

    print("\n" + "=" * 60)
    if all_passed:
        print("FINAL VERDICT: PASS")
        print("All determinism checks passed.")
    else:
        print("FINAL VERDICT: FAIL")
        failed_fixtures = [c.name for c in fixture_checks if not c.passed]
        if failed_fixtures:
            print(f"Failed fixtures: {', '.join(failed_fixtures)}")
        if not backtest_check.passed:
            print("Backtest check failed.")
    print("=" * 60)


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Determinism Gate v1 — verify all fixtures and backtest are deterministic"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output including digest values",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Minimal output, only show final verdict",
    )
    args = parser.parse_args()

    verbose = args.verbose and not args.quiet

    # Discover fixtures
    fixtures = discover_fixtures()

    if not args.quiet:
        print("Determinism Gate v1")
        print(f"Discovered {len(fixtures)} fixtures\n")

    # Check each fixture
    fixture_checks: list[FixtureCheck] = []
    for fixture_path in fixtures:
        if not args.quiet:
            print(f"Checking {fixture_path.name}...")
        check = check_fixture(fixture_path, verbose=verbose)
        fixture_checks.append(check)
        if not args.quiet:
            status = "PASS" if check.passed else "FAIL"
            print(f"  {status}")

    # Check backtest
    if not args.quiet:
        print("\nChecking backtest determinism...")
    backtest_check = check_backtest(verbose=verbose)
    if not args.quiet:
        status = "PASS" if backtest_check.passed else "FAIL"
        print(f"  {status}")

    # Print summary
    if not args.quiet:
        print_summary(fixture_checks, backtest_check, verbose=verbose)

    # Exit with appropriate code
    all_fixtures_passed = all(c.passed for c in fixture_checks)
    if all_fixtures_passed and backtest_check.passed:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
