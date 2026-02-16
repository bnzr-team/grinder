#!/usr/bin/env python3
"""Determinism Gate v1 -- verify all fixtures and backtest are deterministic.

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
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from grinder.execution import SymbolConstraints
from grinder.paper import PaperEngine
from grinder.policies.grid.adaptive import AdaptiveGridConfig
from grinder.replay import ReplayEngine
from grinder.selection import TopKConfigV1

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
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Check if backtest determinism passed."""
        return self.match and self.all_fixtures_passed and not self.errors


@dataclass
class FixtureConfig:
    """Parsed configuration for a fixture."""

    controller_enabled: bool
    feature_engine_enabled: bool
    adaptive_policy_enabled: bool
    topk_v1_enabled: bool
    topk_v1_config: TopKConfigV1 | None
    constraints_enabled: bool
    constraint_cache_path: Path | None
    symbol_constraints: dict[str, dict[str, Any]] | None
    l2_execution_guard_enabled: bool
    l2_execution_max_age_ms: int
    l2_execution_impact_threshold_bps: int
    size_per_level: Decimal | None
    max_notional_per_symbol: Decimal | None
    max_notional_total: Decimal | None
    adaptive_config: AdaptiveGridConfig | None
    replay_expected: str
    paper_expected: str
    # M8: ML signal integration
    ml_enabled: bool
    # M8-02a: ONNX artifact plumbing
    ml_shadow_mode: bool
    ml_infer_enabled: bool
    onnx_artifact_dir: str | None
    # M8-03c-2: ML registry wiring
    ml_registry_path: str | None
    ml_model_name: str | None
    ml_stage: str


def parse_fixture_config(fixture_path: Path, config: dict[str, Any]) -> FixtureConfig:
    """Parse fixture configuration into FixtureConfig."""
    controller_enabled = bool(config.get("controller_enabled", False))
    feature_engine_enabled = bool(config.get("feature_engine_enabled", False))
    adaptive_policy_enabled = bool(config.get("adaptive_policy_enabled", False))
    topk_v1_enabled = bool(config.get("topk_v1_enabled", False))

    # Build TopKConfigV1 if enabled
    topk_v1_config = None
    if topk_v1_enabled:
        topk_v1_config = TopKConfigV1(
            k=config.get("topk_v1_k", 3),
            spread_max_bps=config.get("topk_v1_spread_max_bps", 100),
            thin_l1_min=config.get("topk_v1_thin_l1_min", 1.0),
            warmup_min=config.get("topk_v1_warmup_min", 10),
        )
        feature_engine_enabled = True

    # M7: Read constraint and L2 guard config
    constraints_enabled = bool(config.get("constraints_enabled", False))
    constraint_cache_file = config.get("constraint_cache_file", "")
    constraint_cache_path: Path | None = None
    if constraint_cache_file:
        constraint_cache_path = fixture_path / constraint_cache_file
    symbol_constraints = config.get("symbol_constraints")
    l2_execution_guard_enabled = bool(config.get("l2_execution_guard_enabled", False))
    l2_execution_max_age_ms = int(config.get("l2_execution_max_age_ms", 1500))
    l2_execution_impact_threshold_bps = int(config.get("l2_execution_impact_threshold_bps", 50))

    # Custom sizing params
    size_per_level_str = config.get("size_per_level")
    size_per_level = Decimal(size_per_level_str) if size_per_level_str else None
    max_notional_str = config.get("max_notional_per_symbol")
    max_notional_per_symbol = Decimal(max_notional_str) if max_notional_str else None
    max_total_str = config.get("max_notional_total")
    max_notional_total = Decimal(max_total_str) if max_total_str else None

    # M7: Build AdaptiveGridConfig only if l2_gating is explicitly enabled
    adaptive_config: AdaptiveGridConfig | None = None
    l2_gating_enabled = bool(config.get("l2_gating_enabled", False))
    if l2_gating_enabled:
        adaptive_config = AdaptiveGridConfig(
            l2_gating_enabled=l2_gating_enabled,
            l2_impact_threshold_bps=int(config.get("l2_impact_threshold_bps", 50)),
        )
        adaptive_policy_enabled = True

    # Expected digests
    replay_expected = config.get("expected_digest", "")
    paper_expected = config.get("expected_paper_digest", "")
    if not paper_expected:
        paper_expected = config.get("canonical_digest", "")

    # M8: ML signal integration (safe-by-default: False)
    ml_enabled = bool(config.get("ml_enabled", False))

    # M8-02a: ONNX artifact plumbing (safe-by-default: all False/None)
    ml_shadow_mode = bool(config.get("ml_shadow_mode", False))
    ml_infer_enabled = bool(config.get("ml_infer_enabled", False))
    onnx_artifact_dir = config.get("onnx_artifact_dir")

    # M8-03c-2: ML registry wiring (safe-by-default: None/shadow)
    ml_registry_path = config.get("ml_registry_path")
    ml_model_name = config.get("ml_model_name")
    ml_stage = config.get("ml_stage", "shadow")

    return FixtureConfig(
        controller_enabled=controller_enabled,
        feature_engine_enabled=feature_engine_enabled,
        adaptive_policy_enabled=adaptive_policy_enabled,
        topk_v1_enabled=topk_v1_enabled,
        topk_v1_config=topk_v1_config,
        constraints_enabled=constraints_enabled,
        constraint_cache_path=constraint_cache_path,
        symbol_constraints=symbol_constraints,
        l2_execution_guard_enabled=l2_execution_guard_enabled,
        l2_execution_max_age_ms=l2_execution_max_age_ms,
        l2_execution_impact_threshold_bps=l2_execution_impact_threshold_bps,
        size_per_level=size_per_level,
        max_notional_per_symbol=max_notional_per_symbol,
        max_notional_total=max_notional_total,
        adaptive_config=adaptive_config,
        replay_expected=replay_expected,
        paper_expected=paper_expected,
        ml_enabled=ml_enabled,
        ml_shadow_mode=ml_shadow_mode,
        ml_infer_enabled=ml_infer_enabled,
        onnx_artifact_dir=onnx_artifact_dir,
        ml_registry_path=ml_registry_path,
        ml_model_name=ml_model_name,
        ml_stage=ml_stage,
    )


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


def run_paper(
    fixture_path: Path,
    controller_enabled: bool = False,
    feature_engine_enabled: bool = False,
    adaptive_policy_enabled: bool = False,
    topk_v1_enabled: bool = False,
    topk_v1_config: TopKConfigV1 | None = None,
    # M7 parameters
    constraints_enabled: bool = False,
    constraint_cache_path: Path | None = None,
    l2_execution_guard_enabled: bool = False,
    l2_execution_max_age_ms: int = 1500,
    l2_execution_impact_threshold_bps: int = 50,
    symbol_constraints: dict[str, dict[str, Any]] | None = None,
    # Custom params for fixture testing
    size_per_level: Decimal | None = None,
    max_notional_per_symbol: Decimal | None = None,
    max_notional_total: Decimal | None = None,
    # M7 L2 gating (AdaptiveGridPolicy)
    adaptive_config: AdaptiveGridConfig | None = None,
    # M8 ML signal integration
    ml_enabled: bool = False,
    # M8-02a ONNX artifact plumbing
    ml_shadow_mode: bool = False,
    ml_infer_enabled: bool = False,
    onnx_artifact_dir: str | None = None,
    # M8-03c-2 ML registry wiring
    ml_registry_path: str | None = None,
    ml_model_name: str | None = None,
    ml_stage: str = "shadow",
) -> str:
    """Run paper trading and return digest."""
    # Convert symbol_constraints dict to SymbolConstraints objects
    parsed_constraints: dict[str, SymbolConstraints] | None = None
    if symbol_constraints:
        parsed_constraints = {
            sym: SymbolConstraints(
                step_size=Decimal(str(c["step_size"])),
                min_qty=Decimal(str(c["min_qty"])),
            )
            for sym, c in symbol_constraints.items()
        }

    # Build kwargs with defaults for optional params
    kwargs: dict[str, Any] = {
        "controller_enabled": controller_enabled,
        "feature_engine_enabled": feature_engine_enabled,
        "adaptive_policy_enabled": adaptive_policy_enabled,
        "topk_v1_enabled": topk_v1_enabled,
        "topk_v1_config": topk_v1_config,
        "constraints_enabled": constraints_enabled,
        "constraint_cache_path": constraint_cache_path,
        "l2_execution_guard_enabled": l2_execution_guard_enabled,
        "l2_execution_max_age_ms": l2_execution_max_age_ms,
        "l2_execution_impact_threshold_bps": l2_execution_impact_threshold_bps,
        "symbol_constraints": parsed_constraints,
        "ml_enabled": ml_enabled,
        "ml_shadow_mode": ml_shadow_mode,
        "ml_infer_enabled": ml_infer_enabled,
        "onnx_artifact_dir": onnx_artifact_dir,
        "ml_registry_path": ml_registry_path,
        "ml_model_name": ml_model_name,
        "ml_stage": ml_stage,
    }
    if size_per_level is not None:
        kwargs["size_per_level"] = size_per_level
    if max_notional_per_symbol is not None:
        kwargs["max_notional_per_symbol"] = max_notional_per_symbol
    if max_notional_total is not None:
        kwargs["max_notional_total"] = max_notional_total
    if adaptive_config is not None:
        kwargs["adaptive_config"] = adaptive_config

    engine = PaperEngine(**kwargs)
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
    fc = parse_fixture_config(fixture_path, config)
    errors: list[str] = []

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
    replay_expected_match = replay_1 == fc.replay_expected if fc.replay_expected else True

    # Run paper twice
    try:
        paper_1 = run_paper(
            fixture_path,
            controller_enabled=fc.controller_enabled,
            feature_engine_enabled=fc.feature_engine_enabled,
            adaptive_policy_enabled=fc.adaptive_policy_enabled,
            topk_v1_enabled=fc.topk_v1_enabled,
            topk_v1_config=fc.topk_v1_config,
            constraints_enabled=fc.constraints_enabled,
            constraint_cache_path=fc.constraint_cache_path,
            l2_execution_guard_enabled=fc.l2_execution_guard_enabled,
            l2_execution_max_age_ms=fc.l2_execution_max_age_ms,
            l2_execution_impact_threshold_bps=fc.l2_execution_impact_threshold_bps,
            symbol_constraints=fc.symbol_constraints,
            size_per_level=fc.size_per_level,
            max_notional_per_symbol=fc.max_notional_per_symbol,
            max_notional_total=fc.max_notional_total,
            adaptive_config=fc.adaptive_config,
            ml_enabled=fc.ml_enabled,
            ml_shadow_mode=fc.ml_shadow_mode,
            ml_infer_enabled=fc.ml_infer_enabled,
            onnx_artifact_dir=fc.onnx_artifact_dir,
            ml_registry_path=fc.ml_registry_path,
            ml_model_name=fc.ml_model_name,
            ml_stage=fc.ml_stage,
        )
        paper_2 = run_paper(
            fixture_path,
            controller_enabled=fc.controller_enabled,
            feature_engine_enabled=fc.feature_engine_enabled,
            adaptive_policy_enabled=fc.adaptive_policy_enabled,
            topk_v1_enabled=fc.topk_v1_enabled,
            topk_v1_config=fc.topk_v1_config,
            constraints_enabled=fc.constraints_enabled,
            constraint_cache_path=fc.constraint_cache_path,
            l2_execution_guard_enabled=fc.l2_execution_guard_enabled,
            l2_execution_max_age_ms=fc.l2_execution_max_age_ms,
            l2_execution_impact_threshold_bps=fc.l2_execution_impact_threshold_bps,
            symbol_constraints=fc.symbol_constraints,
            size_per_level=fc.size_per_level,
            max_notional_per_symbol=fc.max_notional_per_symbol,
            max_notional_total=fc.max_notional_total,
            adaptive_config=fc.adaptive_config,
            ml_enabled=fc.ml_enabled,
            ml_shadow_mode=fc.ml_shadow_mode,
            ml_infer_enabled=fc.ml_infer_enabled,
            onnx_artifact_dir=fc.onnx_artifact_dir,
            ml_registry_path=fc.ml_registry_path,
            ml_model_name=fc.ml_model_name,
            ml_stage=fc.ml_stage,
        )
    except Exception as e:
        errors.append(f"Paper error: {e}")
        paper_1 = paper_2 = ""

    paper_match = paper_1 == paper_2 and paper_1 != ""
    paper_expected_match = paper_1 == fc.paper_expected if fc.paper_expected else True

    return FixtureCheck(
        name=name,
        replay_digest_1=replay_1,
        replay_digest_2=replay_2,
        replay_match=replay_match,
        replay_expected=fc.replay_expected,
        replay_expected_match=replay_expected_match,
        paper_digest_1=paper_1,
        paper_digest_2=paper_2,
        paper_match=paper_match,
        paper_expected=fc.paper_expected,
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
        description="Determinism Gate v1 -- verify all fixtures and backtest are deterministic"
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
