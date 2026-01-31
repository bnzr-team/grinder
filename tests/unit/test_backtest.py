"""Contract tests for backtest protocol report schema.

These tests verify that the backtest report schema remains stable and
that digest validation works correctly.

Contract guarantees:
- FixtureResult: fixture_path, schema_version, paper_digest, expected_paper_digest,
  digest_match, total_fills, final_positions, total_realized_pnl, total_unrealized_pnl,
  events_processed, orders_placed, orders_blocked, errors
- BacktestReport: report_schema_version, paper_schema_version, fixtures_run,
  fixtures_passed, fixtures_failed, all_digests_match, results, report_digest
- Digest matching is deterministic (same fixtures -> same report_digest)
- All monetary values are strings (Decimal serialization)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

# Add project root to path for scripts import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.run_backtest import (
    FIXTURES,
    REPORT_SCHEMA_VERSION,
    BacktestReport,
    FixtureResult,
    load_fixture_config,
    run_backtest,
    run_fixture,
)

from grinder.paper import SCHEMA_VERSION


class TestReportSchemaVersion:
    """Tests for report schema versioning."""

    def test_report_schema_version_is_v1(self) -> None:
        """Verify current report schema version is v1."""
        assert REPORT_SCHEMA_VERSION == "v1"

    def test_report_includes_schema_versions(self) -> None:
        """Verify report includes both report and paper schema versions."""
        report = run_backtest()
        assert report.report_schema_version == "v1"
        assert report.paper_schema_version == SCHEMA_VERSION


class TestFixtureResultContract:
    """Tests for FixtureResult schema contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {
        "fixture_path",
        "schema_version",
        "paper_digest",
        "expected_paper_digest",
        "digest_match",
        "total_fills",
        "final_positions",
        "total_realized_pnl",
        "total_unrealized_pnl",
        "events_processed",
        "orders_placed",
        "orders_blocked",
        "errors",
    }

    def test_fixture_result_has_all_required_keys(self) -> None:
        """Verify FixtureResult.to_dict() has all required keys."""
        result = FixtureResult(
            fixture_path="/test",
            schema_version="v1",
            paper_digest="abc123",
            expected_paper_digest="abc123",
            digest_match=True,
            total_fills=5,
            final_positions={},
            total_realized_pnl="100.5",
            total_unrealized_pnl="-50.25",
            events_processed=10,
            orders_placed=5,
            orders_blocked=0,
            errors=[],
        )
        d = result.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing required keys: {missing}"

    def test_fixture_result_from_engine_has_all_keys(self) -> None:
        """Verify real fixture result contains all required keys."""
        fixture_path = FIXTURES[0]
        if fixture_path.exists():
            result = run_fixture(fixture_path)
            d = result.to_dict()
            missing = self.REQUIRED_KEYS - set(d.keys())
            assert not missing, f"Missing keys in result: {missing}"

    def test_pnl_values_are_strings(self) -> None:
        """Verify PnL values are serialized as strings."""
        fixture_path = FIXTURES[0]
        if fixture_path.exists():
            result = run_fixture(fixture_path)
            d = result.to_dict()
            assert isinstance(d["total_realized_pnl"], str)
            assert isinstance(d["total_unrealized_pnl"], str)


class TestBacktestReportContract:
    """Tests for BacktestReport schema contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {
        "report_schema_version",
        "paper_schema_version",
        "fixtures_run",
        "fixtures_passed",
        "fixtures_failed",
        "all_digests_match",
        "results",
        "report_digest",
    }

    def test_report_has_all_required_keys(self) -> None:
        """Verify BacktestReport.to_dict() has all required keys."""
        report = BacktestReport(
            report_schema_version="v1",
            paper_schema_version="v1",
            fixtures_run=2,
            fixtures_passed=2,
            fixtures_failed=0,
            all_digests_match=True,
            results=[],
            report_digest="abc123",
        )
        d = report.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing required keys: {missing}"

    def test_report_from_backtest_has_all_keys(self) -> None:
        """Verify real backtest report contains all required keys."""
        report = run_backtest()
        d = report.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing keys in report: {missing}"

    def test_results_is_list(self) -> None:
        """Verify results field is a list."""
        report = run_backtest()
        assert isinstance(report.results, list)

    def test_report_digest_is_nonempty(self) -> None:
        """Verify report_digest is computed and non-empty."""
        report = run_backtest()
        assert report.report_digest
        assert len(report.report_digest) == 16  # Truncated SHA256


class TestDigestValidation:
    """Tests for digest matching against fixture configs."""

    def test_digests_match_fixture_configs(self) -> None:
        """Verify paper digests match expected values in fixture configs."""
        report = run_backtest()

        for result in report.results:
            if result.expected_paper_digest:
                assert result.digest_match, (
                    f"Digest mismatch for {result.fixture_path}: "
                    f"got {result.paper_digest}, expected {result.expected_paper_digest}"
                )

    def test_all_digests_match_flag(self) -> None:
        """Verify all_digests_match reflects actual digest status."""
        report = run_backtest()

        # all_digests_match should be True iff all individual digest_match are True
        expected = all(r.digest_match for r in report.results)
        assert report.all_digests_match == expected

    def test_fixture_config_has_expected_digest(self) -> None:
        """Verify fixture configs have expected_paper_digest field."""
        for fixture_path in FIXTURES:
            if fixture_path.exists():
                config = load_fixture_config(fixture_path)
                assert "expected_paper_digest" in config, (
                    f"Fixture {fixture_path} missing expected_paper_digest in config.json"
                )


class TestDeterminism:
    """Tests for backtest report determinism."""

    def test_same_fixtures_produce_same_report_digest(self) -> None:
        """Verify same fixtures produce identical report digest across runs."""
        digests = []
        for _ in range(3):
            report = run_backtest()
            digests.append(report.report_digest)

        assert len(set(digests)) == 1, f"Report digests differ: {digests}"

    def test_same_fixtures_produce_same_json(self) -> None:
        """Verify same fixtures produce identical JSON output across runs."""
        outputs = []
        for _ in range(2):
            report = run_backtest()
            outputs.append(report.to_json())

        assert outputs[0] == outputs[1], "JSON outputs differ between runs"

    def test_json_is_deterministic_sorted(self) -> None:
        """Verify JSON output uses sorted keys."""
        report = run_backtest()
        json_str = report.to_json()

        # Parse and re-serialize with sort_keys to verify
        parsed = json.loads(json_str)
        reserialized = json.dumps(parsed, sort_keys=True, separators=(",", ":"))

        assert json_str == reserialized, "JSON is not deterministically sorted"


class TestFixtureRunCounts:
    """Tests for fixture run counting."""

    def test_fixtures_run_matches_results_length(self) -> None:
        """Verify fixtures_run equals number of results."""
        report = run_backtest()
        assert report.fixtures_run == len(report.results)

    def test_passed_plus_failed_equals_run(self) -> None:
        """Verify passed + failed = run."""
        report = run_backtest()
        assert report.fixtures_passed + report.fixtures_failed == report.fixtures_run

    def test_runs_exactly_two_fixtures(self) -> None:
        """Verify backtest runs exactly 2 registered fixtures."""
        report = run_backtest()
        assert report.fixtures_run == 2
        assert len(FIXTURES) == 2


class TestErrorHandling:
    """Tests for error handling in backtest."""

    def test_missing_fixture_reports_error(self) -> None:
        """Verify missing fixture is reported with error."""
        report = run_backtest([Path("nonexistent/fixture")])

        assert report.fixtures_run == 1
        assert report.fixtures_failed == 1
        assert len(report.results) == 1

        result = report.results[0]
        assert not result.digest_match
        assert len(result.errors) > 0
        assert "not found" in result.errors[0].lower()

    def test_partial_failure_continues(self) -> None:
        """Verify backtest continues after one fixture fails."""
        fixtures = [Path("nonexistent"), FIXTURES[0]]
        report = run_backtest(fixtures)

        assert report.fixtures_run == 2
        assert report.fixtures_failed >= 1
        # Second fixture should still be processed
        assert len(report.results) == 2
