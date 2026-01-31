#!/usr/bin/env python3
"""Run backtest protocol on fixture suite and generate report.

This script runs paper trading v1 on all registered fixtures and produces
a deterministic JSON report with digests, positions, and PnL.

Usage:
    python -m scripts.run_backtest
    python -m scripts.run_backtest --out report.json
    python -m scripts.run_backtest --out report.json --quiet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grinder.paper import SCHEMA_VERSION, PaperEngine

# Registered fixtures for backtest protocol
FIXTURES = [
    Path("tests/fixtures/sample_day"),
    Path("tests/fixtures/sample_day_allowed"),
    Path("tests/fixtures/sample_day_toxic"),
    Path("tests/fixtures/sample_day_multisymbol"),
]

# Report schema version
REPORT_SCHEMA_VERSION = "v1"


@dataclass
class FixtureResult:
    """Result for a single fixture run."""

    fixture_path: str
    schema_version: str
    paper_digest: str
    expected_paper_digest: str
    digest_match: bool
    total_fills: int
    final_positions: dict[str, dict[str, Any]]
    total_realized_pnl: str
    total_unrealized_pnl: str
    events_processed: int
    orders_placed: int
    orders_blocked: int
    errors: list[str] = field(default_factory=list)
    # Top-K prefilter results (v1 addition - ADR-010)
    topk_selected_symbols: list[str] = field(default_factory=list)
    topk_k: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "fixture_path": self.fixture_path,
            "schema_version": self.schema_version,
            "paper_digest": self.paper_digest,
            "expected_paper_digest": self.expected_paper_digest,
            "digest_match": self.digest_match,
            "total_fills": self.total_fills,
            "final_positions": self.final_positions,
            "total_realized_pnl": self.total_realized_pnl,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "events_processed": self.events_processed,
            "orders_placed": self.orders_placed,
            "orders_blocked": self.orders_blocked,
            "errors": self.errors,
            "topk_selected_symbols": self.topk_selected_symbols,
            "topk_k": self.topk_k,
        }


@dataclass
class BacktestReport:
    """Complete backtest report for all fixtures."""

    report_schema_version: str
    paper_schema_version: str
    fixtures_run: int
    fixtures_passed: int
    fixtures_failed: int
    all_digests_match: bool
    results: list[FixtureResult] = field(default_factory=list)
    report_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "report_schema_version": self.report_schema_version,
            "paper_schema_version": self.paper_schema_version,
            "fixtures_run": self.fixtures_run,
            "fixtures_passed": self.fixtures_passed,
            "fixtures_failed": self.fixtures_failed,
            "all_digests_match": self.all_digests_match,
            "results": [r.to_dict() for r in self.results],
            "report_digest": self.report_digest,
        }

    def to_json(self) -> str:
        """Serialize to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_json_pretty(self) -> str:
        """Serialize to human-readable JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)


def load_fixture_config(fixture_path: Path) -> dict[str, Any]:
    """Load fixture config.json to get expected digests."""
    config_path = fixture_path / "config.json"
    if config_path.exists():
        with config_path.open() as f:
            result: dict[str, Any] = json.load(f)
            return result
    return {}


def run_fixture(fixture_path: Path) -> FixtureResult:
    """Run paper trading on a single fixture and return result."""
    config = load_fixture_config(fixture_path)
    expected_digest = config.get("expected_paper_digest", "")

    engine = PaperEngine()
    result = engine.run(fixture_path)

    digest_match = result.digest == expected_digest if expected_digest else True

    return FixtureResult(
        fixture_path=str(fixture_path),
        schema_version=result.schema_version,
        paper_digest=result.digest,
        expected_paper_digest=expected_digest,
        digest_match=digest_match,
        total_fills=result.total_fills,
        final_positions=result.final_positions,
        total_realized_pnl=result.total_realized_pnl,
        total_unrealized_pnl=result.total_unrealized_pnl,
        events_processed=result.events_processed,
        orders_placed=result.orders_placed,
        orders_blocked=result.orders_blocked,
        errors=result.errors,
        topk_selected_symbols=result.topk_selected_symbols,
        topk_k=result.topk_k,
    )


def run_backtest(fixtures: list[Path] | None = None) -> BacktestReport:
    """Run backtest on all fixtures and generate report."""
    if fixtures is None:
        fixtures = FIXTURES

    results: list[FixtureResult] = []
    passed = 0
    failed = 0

    for fixture_path in fixtures:
        if not fixture_path.exists():
            results.append(
                FixtureResult(
                    fixture_path=str(fixture_path),
                    schema_version=SCHEMA_VERSION,
                    paper_digest="",
                    expected_paper_digest="",
                    digest_match=False,
                    total_fills=0,
                    final_positions={},
                    total_realized_pnl="0",
                    total_unrealized_pnl="0",
                    events_processed=0,
                    orders_placed=0,
                    orders_blocked=0,
                    errors=[f"Fixture not found: {fixture_path}"],
                    topk_selected_symbols=[],
                    topk_k=0,
                )
            )
            failed += 1
            continue

        fixture_result = run_fixture(fixture_path)
        results.append(fixture_result)

        if fixture_result.digest_match and not fixture_result.errors:
            passed += 1
        else:
            failed += 1

    all_match = all(r.digest_match for r in results)

    report = BacktestReport(
        report_schema_version=REPORT_SCHEMA_VERSION,
        paper_schema_version=SCHEMA_VERSION,
        fixtures_run=len(results),
        fixtures_passed=passed,
        fixtures_failed=failed,
        all_digests_match=all_match,
        results=results,
    )

    # Compute report digest (excluding digest field itself for determinism)
    report_content = json.dumps(
        {k: v for k, v in report.to_dict().items() if k != "report_digest"},
        sort_keys=True,
        separators=(",", ":"),
    )
    report.report_digest = hashlib.sha256(report_content.encode()).hexdigest()[:16]

    return report


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Run backtest protocol on fixture suite")
    parser.add_argument(
        "--out",
        type=Path,
        help="Write report to file (JSON)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress stdout output (only write to --out if specified)",
    )
    args = parser.parse_args()

    report = run_backtest()

    json_output = report.to_json_pretty()

    # Always print to stdout unless --quiet
    if not args.quiet:
        print(json_output)

    # Write to file if --out specified
    if args.out:
        args.out.write_text(json_output)
        if not args.quiet:
            print(f"\nReport written to: {args.out}", file=sys.stderr)

    # Exit with error if any fixtures failed
    if report.fixtures_failed > 0 or not report.all_digests_match:
        sys.exit(1)


if __name__ == "__main__":
    main()
