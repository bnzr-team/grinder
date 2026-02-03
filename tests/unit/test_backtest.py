"""Contract tests for backtest protocol report schema.

These tests verify that the backtest report schema remains stable and
that digest validation works correctly.

Contract guarantees:
- FixtureResult: fixture_path, schema_version, paper_digest, expected_paper_digest,
  digest_match, total_fills, final_positions, total_realized_pnl, total_unrealized_pnl,
  events_processed, orders_placed, orders_blocked, errors, topk_selected_symbols, topk_k
- BacktestReport: report_schema_version, paper_schema_version, fixtures_run,
  fixtures_passed, fixtures_failed, all_digests_match, results, report_digest
- Digest matching is deterministic (same fixtures -> same report_digest)
- All monetary values are strings (Decimal serialization)
- Top-K selection is deterministic and included in results (ADR-010)
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

from grinder.paper import SCHEMA_VERSION, PaperEngine


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
        # Top-K prefilter results (ADR-010)
        "topk_selected_symbols",
        "topk_k",
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
            topk_selected_symbols=["BTCUSDT", "ETHUSDT"],
            topk_k=3,
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

    def test_runs_exactly_five_fixtures(self) -> None:
        """Verify backtest runs exactly 5 registered fixtures."""
        report = run_backtest()
        assert report.fixtures_run == 5
        assert len(FIXTURES) == 5


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


class TestTopKContract:
    """Contract tests for Top-K prefilter in backtest results.

    These tests verify the Top-K selection contract (ADR-010):
    - topk_selected_symbols is ordered list of selected symbols
    - topk_k is the K value used for selection
    - Selection is deterministic based on volatility scores
    """

    def test_topk_fields_present_in_results(self) -> None:
        """Verify topk fields are present in all fixture results."""
        report = run_backtest()

        for result in report.results:
            d = result.to_dict()
            assert "topk_selected_symbols" in d
            assert "topk_k" in d
            assert isinstance(d["topk_selected_symbols"], list)
            assert isinstance(d["topk_k"], int)

    def test_topk_k_is_positive(self) -> None:
        """Verify topk_k is a positive integer."""
        report = run_backtest()

        for result in report.results:
            assert result.topk_k > 0

    def test_topk_selected_symbols_is_list_of_strings(self) -> None:
        """Verify topk_selected_symbols contains strings."""
        report = run_backtest()

        for result in report.results:
            for symbol in result.topk_selected_symbols:
                assert isinstance(symbol, str)

    def test_multisymbol_fixture_has_expected_topk(self) -> None:
        """Verify sample_day_multisymbol selects correct top 3 symbols."""
        report = run_backtest()

        # Find the multisymbol fixture result
        multisymbol_result = None
        for result in report.results:
            if "multisymbol" in result.fixture_path:
                multisymbol_result = result
                break

        assert multisymbol_result is not None, "sample_day_multisymbol not found"
        assert multisymbol_result.topk_k == 3
        assert multisymbol_result.topk_selected_symbols == [
            "AAAUSDT",
            "BBBUSDT",
            "CCCUSDT",
        ]

    def test_existing_fixtures_select_all_symbols(self) -> None:
        """Verify existing fixtures with ≤3 symbols have all selected."""
        report = run_backtest()

        expected_selections = {
            "sample_day": {"BTCUSDT", "ETHUSDT"},
            "sample_day_allowed": {"TESTUSDT", "TEST2USDT"},
            "sample_day_toxic": {"TESTUSDT"},
        }

        for result in report.results:
            fixture_name = Path(result.fixture_path).name
            if fixture_name in expected_selections:
                actual = set(result.topk_selected_symbols)
                expected = expected_selections[fixture_name]
                assert actual == expected, f"{fixture_name}: expected {expected}, got {actual}"

    def test_topk_selection_is_deterministic(self) -> None:
        """Verify Top-K selection is identical across backtest runs."""
        reports = [run_backtest() for _ in range(3)]

        # Compare topk_selected_symbols for each fixture across runs
        for i in range(len(reports[0].results)):
            selections = [r.results[i].topk_selected_symbols for r in reports]
            assert all(s == selections[0] for s in selections), (
                f"Non-deterministic Top-K for fixture {reports[0].results[i].fixture_path}"
            )


class TestControllerContract:
    """Tests for Adaptive Controller contract (ADR-011).

    Controller is opt-in; when disabled, controller fields are empty/defaults.
    When enabled, controller decisions are recorded for each symbol.
    """

    def test_controller_respects_fixture_config(self) -> None:
        """Verify controller_enabled is read from fixture config.json."""
        report = run_backtest()

        for result in report.results:
            result_dict = result.to_dict()
            if "sample_day_controller" in result_dict["fixture_path"]:
                # sample_day_controller has controller_enabled=true in config
                assert result_dict["controller_enabled"] is True
            else:
                # Other fixtures don't have controller_enabled set, default is False
                assert result_dict["controller_enabled"] is False

    def test_controller_fixture_canonical_digest(self) -> None:
        """Verify sample_day_controller fixture has expected digest."""
        engine = PaperEngine(controller_enabled=True)
        result = engine.run(Path("tests/fixtures/sample_day_controller"))

        assert result.digest == "f3a0a321c39cc411"

    def test_controller_decisions_schema(self) -> None:
        """Verify controller decision schema is correct."""
        engine = PaperEngine(controller_enabled=True)
        result = engine.run(Path("tests/fixtures/sample_day_controller"))

        assert result.controller_enabled is True
        assert len(result.controller_decisions) > 0

        # Check decision schema
        for decision in result.controller_decisions:
            assert "symbol" in decision
            assert "mode" in decision
            assert "reason" in decision
            assert "spacing_multiplier" in decision
            assert "vol_bps" in decision
            assert "spread_bps_max" in decision
            assert "window_size" in decision

            # Integer fields should be integers
            assert isinstance(decision["vol_bps"], int)
            assert isinstance(decision["spread_bps_max"], int)
            assert isinstance(decision["window_size"], int)

    def test_controller_modes_triggered(self) -> None:
        """Verify sample_day_controller triggers expected modes."""
        engine = PaperEngine(controller_enabled=True)
        result = engine.run(Path("tests/fixtures/sample_day_controller"))

        decisions = {d["symbol"]: d for d in result.controller_decisions}

        # WIDENUSDT should trigger WIDEN mode
        assert decisions["WIDENUSDT"]["mode"] == "WIDEN"
        assert decisions["WIDENUSDT"]["reason"] == "HIGH_VOL"

        # TIGHTENUSDT should trigger TIGHTEN mode
        assert decisions["TIGHTENUSDT"]["mode"] == "TIGHTEN"
        assert decisions["TIGHTENUSDT"]["reason"] == "LOW_VOL"

        # BASEUSDT should stay in BASE mode
        assert decisions["BASEUSDT"]["mode"] == "BASE"
        assert decisions["BASEUSDT"]["reason"] == "NORMAL"

    def test_controller_determinism(self) -> None:
        """Verify controller decisions are deterministic across runs."""
        digests = []
        decisions_list = []
        for _ in range(3):
            engine = PaperEngine(controller_enabled=True)
            result = engine.run(Path("tests/fixtures/sample_day_controller"))
            digests.append(result.digest)
            decisions_list.append(result.controller_decisions)

        # All digests should be identical
        assert all(d == digests[0] for d in digests)

        # All decision lists should be identical
        for decisions in decisions_list:
            assert decisions == decisions_list[0]

    def test_existing_digests_preserved_with_controller_disabled(self) -> None:
        """Verify existing canonical digests unchanged when controller disabled.

        Note: Digests updated in PR-ASM-P0-01 due to crossing/touch fill model (v1.1).
        sample_day unchanged (0 fills - blocked by gating).
        """
        expected = {
            "sample_day": "66b29a4e92192f8f",  # blocked by gating, 0 fills
            "sample_day_allowed": "3ecf49cd03db1b07",  # v1.1 crossing/touch fills
            "sample_day_toxic": "a31ead72fc1f197e",  # v1.1 crossing/touch fills
            "sample_day_multisymbol": "22acba5cb8b81ab4",  # v1.1 crossing/touch fills
        }

        for fixture, expected_digest in expected.items():
            engine = PaperEngine(controller_enabled=False)
            result = engine.run(Path(f"tests/fixtures/{fixture}"))
            assert result.digest == expected_digest, (
                f"Digest mismatch for {fixture}: expected {expected_digest}, got {result.digest}"
            )
