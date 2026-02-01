"""Unit tests for soak threshold evaluation.

Tests the check_soak_thresholds.py script logic:
- Simple max thresholds
- Min/max range thresholds
- Missing metrics handling
- Violation detection
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Import the module under test (scripts directory)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
from check_soak_thresholds import (  # type: ignore[import-not-found]
    check_thresholds,
    load_json,
    load_yaml,
)


class TestCheckThresholds:
    """Tests for check_thresholds function."""

    def test_simple_max_threshold_pass(self) -> None:
        """Value below max threshold passes."""
        results = {"latency_ms": 50}
        thresholds = {"baseline": {"latency_ms": 100}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert violations == []

    def test_simple_max_threshold_fail(self) -> None:
        """Value above max threshold fails."""
        results = {"latency_ms": 150}
        thresholds = {"baseline": {"latency_ms": 100}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert len(violations) == 1
        assert "latency_ms" in violations[0]
        assert "150" in violations[0]
        assert "100" in violations[0]

    def test_exact_threshold_passes(self) -> None:
        """Value exactly at threshold passes (not strictly greater)."""
        results = {"latency_ms": 100}
        thresholds = {"baseline": {"latency_ms": 100}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert violations == []

    def test_min_max_range_pass(self) -> None:
        """Value within min/max range passes."""
        results = {"fill_rate": 0.6}
        thresholds = {"baseline": {"fill_rate": {"min": 0.4, "max": 1.0}}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert violations == []

    def test_min_max_range_below_min(self) -> None:
        """Value below min fails."""
        results = {"fill_rate": 0.2}
        thresholds = {"baseline": {"fill_rate": {"min": 0.4, "max": 1.0}}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert len(violations) == 1
        assert "fill_rate" in violations[0]
        assert "min" in violations[0]

    def test_min_max_range_above_max(self) -> None:
        """Value above max fails."""
        results = {"fill_rate": 1.5}
        thresholds = {"baseline": {"fill_rate": {"min": 0.4, "max": 1.0}}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert len(violations) == 1
        assert "fill_rate" in violations[0]
        assert "max" in violations[0]

    def test_missing_metric_ignored(self) -> None:
        """Metric not in results is ignored."""
        results = {"latency_ms": 50}
        thresholds = {"baseline": {"latency_ms": 100, "memory_mb": 512}}

        violations = check_thresholds(results, thresholds, "baseline")

        assert violations == []

    def test_unknown_mode_returns_empty(self) -> None:
        """Unknown mode returns no violations (no thresholds defined)."""
        results = {"latency_ms": 999}
        thresholds = {"baseline": {"latency_ms": 100}}

        violations = check_thresholds(results, thresholds, "unknown_mode")

        assert violations == []

    def test_multiple_violations(self) -> None:
        """Multiple threshold violations are all reported."""
        results = {
            "latency_ms": 200,
            "memory_mb": 2000,
            "errors": 10,
        }
        thresholds = {
            "baseline": {
                "latency_ms": 100,
                "memory_mb": 512,
                "errors": 0,
            }
        }

        violations = check_thresholds(results, thresholds, "baseline")

        assert len(violations) == 3

    def test_overload_mode_separate_thresholds(self) -> None:
        """Overload mode uses different thresholds."""
        results = {"latency_ms": 150}
        thresholds = {
            "baseline": {"latency_ms": 100},
            "overload": {"latency_ms": 200},
        }

        baseline_violations = check_thresholds(results, thresholds, "baseline")
        overload_violations = check_thresholds(results, thresholds, "overload")

        assert len(baseline_violations) == 1  # 150 > 100
        assert len(overload_violations) == 0  # 150 <= 200


class TestLoadFunctions:
    """Tests for file loading functions."""

    def test_load_json(self, tmp_path: Path) -> None:
        """Load JSON file correctly."""
        data = {"key": "value", "number": 42}
        json_file = tmp_path / "test.json"
        json_file.write_text(json.dumps(data))

        result = load_json(json_file)

        assert result == data

    def test_load_yaml(self, tmp_path: Path) -> None:
        """Load YAML file correctly."""
        yaml_content = """
baseline:
  latency_ms: 100
  fill_rate:
    min: 0.4
    max: 1.0
"""
        yaml_file = tmp_path / "test.yml"
        yaml_file.write_text(yaml_content)

        result = load_yaml(yaml_file)

        assert result["baseline"]["latency_ms"] == 100
        assert result["baseline"]["fill_rate"]["min"] == 0.4


class TestSoakThresholdsContract:
    """Contract tests for soak thresholds configuration."""

    @pytest.fixture
    def thresholds(self) -> dict[str, Any]:
        """Load actual thresholds file."""
        thresholds_path = Path("monitoring/soak_thresholds.yml")
        if not thresholds_path.exists():
            pytest.skip("Thresholds file not found")
        result: dict[str, Any] = load_yaml(thresholds_path)
        return result

    def test_baseline_mode_exists(self, thresholds: dict[str, Any]) -> None:
        """Baseline mode must be defined."""
        assert "baseline" in thresholds

    def test_overload_mode_exists(self, thresholds: dict[str, Any]) -> None:
        """Overload mode must be defined."""
        assert "overload" in thresholds

    def test_required_baseline_metrics(self, thresholds: dict[str, Any]) -> None:
        """Baseline must define core metrics."""
        baseline = thresholds["baseline"]
        required = [
            "decision_latency_p99_ms",
            "order_latency_p99_ms",
            "errors_total",
            "rss_mb_max",
            "fill_rate",
        ]
        for metric in required:
            assert metric in baseline, f"Missing baseline metric: {metric}"

    def test_required_overload_metrics(self, thresholds: dict[str, Any]) -> None:
        """Overload must define core metrics."""
        overload = thresholds["overload"]
        required = [
            "decision_latency_p99_ms",
            "order_latency_p99_ms",
            "errors_total",
            "rss_mb_max",
            "fill_rate",
        ]
        for metric in required:
            assert metric in overload, f"Missing overload metric: {metric}"

    def test_overload_more_lenient(self, thresholds: dict[str, Any]) -> None:
        """Overload thresholds should be more lenient than baseline."""
        baseline = thresholds["baseline"]
        overload = thresholds["overload"]

        # Latency thresholds should be higher for overload
        assert overload["decision_latency_p99_ms"] >= baseline["decision_latency_p99_ms"]
        assert overload["order_latency_p99_ms"] >= baseline["order_latency_p99_ms"]

        # Error tolerance should be higher for overload
        assert overload["errors_total"] >= baseline["errors_total"]

        # Memory should be same or higher for overload
        assert overload["rss_mb_max"] >= baseline["rss_mb_max"]

    def test_fill_rate_has_min_max(self, thresholds: dict[str, Any]) -> None:
        """Fill rate should have min/max range."""
        for mode in ["baseline", "overload"]:
            fill_rate = thresholds[mode]["fill_rate"]
            assert isinstance(fill_rate, dict)
            assert "min" in fill_rate
            assert "max" in fill_rate
            assert 0 <= fill_rate["min"] <= fill_rate["max"] <= 1.0


class TestSoakFixturesRunner:
    """Tests for run_soak_fixtures module."""

    @pytest.fixture(autouse=True)
    def setup_soak_imports(self) -> None:
        """Ensure scripts path is in sys.path for all tests."""
        scripts_path = str(Path(__file__).parent.parent.parent / "scripts")
        if scripts_path not in sys.path:
            sys.path.insert(0, scripts_path)

    def test_fixture_soak_result_latency_p50(self) -> None:
        """P50 latency calculation."""
        from run_soak_fixtures import (  # type: ignore[import-not-found]  # noqa: PLC0415
            FixtureSoakResult,
        )

        result = FixtureSoakResult(
            fixture_path="test",
            runs=5,
            latencies_ms=[10, 20, 30, 40, 50],
        )

        assert result.latency_p50_ms == 30  # Median of [10,20,30,40,50]

    def test_fixture_soak_result_latency_p99(self) -> None:
        """P99 latency calculation."""
        from run_soak_fixtures import FixtureSoakResult  # noqa: PLC0415

        result = FixtureSoakResult(
            fixture_path="test",
            runs=100,
            latencies_ms=list(range(1, 101)),  # 1 to 100
        )

        # P99 of 1-100 should be around 99
        assert result.latency_p99_ms >= 99

    def test_fixture_soak_result_digest_stable_all_same(self) -> None:
        """Digest stable when all digests match."""
        from run_soak_fixtures import FixtureSoakResult  # noqa: PLC0415

        result = FixtureSoakResult(
            fixture_path="test",
            runs=3,
            digests=["abc123", "abc123", "abc123"],
        )

        assert result.digest_stable is True

    def test_fixture_soak_result_digest_unstable(self) -> None:
        """Digest unstable when digests differ."""
        from run_soak_fixtures import FixtureSoakResult  # noqa: PLC0415

        result = FixtureSoakResult(
            fixture_path="test",
            runs=3,
            digests=["abc123", "abc123", "def456"],
        )

        assert result.digest_stable is False

    def test_fixture_soak_result_to_dict(self) -> None:
        """Result serializes to dict correctly."""
        from run_soak_fixtures import FixtureSoakResult  # noqa: PLC0415

        result = FixtureSoakResult(
            fixture_path="tests/fixtures/sample_day",
            runs=3,
            latencies_ms=[10, 20, 30],
            fill_counts=[5, 5, 5],
            orders_placed_counts=[5, 5, 5],  # Same as fills = 100% fill rate
            errors=[],
            digests=["abc", "abc", "abc"],
        )

        d = result.to_dict()

        assert d["fixture_path"] == "tests/fixtures/sample_day"
        assert d["runs"] == 3
        assert d["latency_p50_ms"] == 20
        assert d["fill_rate"] == 1.0  # 15 fills / 15 orders = 1.0
        assert d["errors_total"] == 0
        assert d["digest_stable"] is True

    def test_soak_report_to_dict(self) -> None:
        """SoakReport serializes with threshold-compatible fields."""
        from run_soak_fixtures import SoakReport  # noqa: PLC0415

        report = SoakReport(
            schema_version="v1",
            mode="baseline",
            fixtures_tested=3,
            total_runs=9,
            decision_latency_p50_ms=15.5,
            decision_latency_p99_ms=45.2,
            rss_mb_max=256.0,
            fill_rate=0.65,
            errors_total=0,
            events_dropped=0,
            all_digests_stable=True,
        )

        d = report.to_dict()

        # Check threshold-compatible fields
        assert "decision_latency_p99_ms" in d
        assert "order_latency_p99_ms" in d
        assert "rss_mb_max" in d
        assert "fill_rate" in d
        assert "errors_total" in d
        assert "events_dropped" in d


class TestCheckSoakGate:
    """Tests for check_soak_gate.py deterministic gate logic."""

    @pytest.fixture(autouse=True)
    def setup_gate_imports(self) -> None:
        """Ensure scripts path is in sys.path for all tests."""
        scripts_path = str(Path(__file__).parent.parent.parent / "scripts")
        if scripts_path not in sys.path:
            sys.path.insert(0, scripts_path)

    @pytest.fixture
    def baseline_thresholds(self) -> dict[str, Any]:
        """Standard baseline thresholds."""
        return {
            "baseline": {
                "errors_total": 0,
                "events_dropped": 0,
                "fill_rate": {"min": 0.4, "max": 1.0},
            }
        }

    def test_stable_report_passes(self, baseline_thresholds: dict[str, Any]) -> None:
        """Report with all stable metrics passes all gates."""
        from check_soak_gate import (  # type: ignore[import-not-found]  # noqa: PLC0415
            check_deterministic_gates,
        )

        report = {
            "all_digests_stable": True,
            "errors_total": 0,
            "fill_rate": 1.0,
            "events_dropped": 0,
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        assert failures == []

    def test_unstable_digests_fail(self, baseline_thresholds: dict[str, Any]) -> None:
        """Report with unstable digests fails."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        report = {
            "all_digests_stable": False,
            "errors_total": 0,
            "fill_rate": 1.0,
            "events_dropped": 0,
            "results": [
                {"fixture_path": "tests/fixtures/sample_day", "digest_stable": False},
            ],
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        assert len(failures) >= 1
        assert any("instability" in f.lower() for f in failures)

    def test_errors_total_fail(self, baseline_thresholds: dict[str, Any]) -> None:
        """Report with errors above threshold fails."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        report = {
            "all_digests_stable": True,
            "errors_total": 5,
            "fill_rate": 1.0,
            "events_dropped": 0,
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        assert len(failures) >= 1
        assert any("errors_total" in f for f in failures)

    def test_fill_rate_below_min_fail(self, baseline_thresholds: dict[str, Any]) -> None:
        """Report with fill_rate below min fails."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        report = {
            "all_digests_stable": True,
            "errors_total": 0,
            "fill_rate": 0.2,  # Below min 0.4
            "events_dropped": 0,
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        assert len(failures) >= 1
        assert any("fill_rate" in f and "min" in f for f in failures)

    def test_fill_rate_above_max_fail(self, baseline_thresholds: dict[str, Any]) -> None:
        """Report with fill_rate above max fails."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        report = {
            "all_digests_stable": True,
            "errors_total": 0,
            "fill_rate": 1.5,  # Above max 1.0
            "events_dropped": 0,
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        assert len(failures) >= 1
        assert any("fill_rate" in f and "max" in f for f in failures)

    def test_events_dropped_fail(self, baseline_thresholds: dict[str, Any]) -> None:
        """Report with events_dropped above threshold fails."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        report = {
            "all_digests_stable": True,
            "errors_total": 0,
            "fill_rate": 1.0,
            "events_dropped": 10,  # Above threshold 0
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        assert len(failures) >= 1
        assert any("events_dropped" in f for f in failures)

    def test_overload_mode_more_lenient(self) -> None:
        """Overload mode allows higher error counts."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        thresholds = {
            "baseline": {"errors_total": 0, "fill_rate": {"min": 0.4, "max": 1.0}},
            "overload": {"errors_total": 10, "fill_rate": {"min": 0.2, "max": 1.0}},
        }

        report = {
            "all_digests_stable": True,
            "errors_total": 5,
            "fill_rate": 0.3,
        }

        baseline_failures = check_deterministic_gates(report, thresholds, "baseline")
        overload_failures = check_deterministic_gates(report, thresholds, "overload")

        # Baseline should fail (5 > 0, 0.3 < 0.4)
        assert len(baseline_failures) >= 2

        # Overload should pass (5 <= 10, 0.3 >= 0.2)
        assert overload_failures == []

    def test_multiple_failures_all_reported(self, baseline_thresholds: dict[str, Any]) -> None:
        """Multiple gate failures are all reported."""
        from check_soak_gate import check_deterministic_gates  # noqa: PLC0415

        report = {
            "all_digests_stable": False,
            "errors_total": 10,
            "fill_rate": 0.1,
            "events_dropped": 50,
            "results": [
                {"fixture_path": "test", "digest_stable": False},
            ],
        }

        failures = check_deterministic_gates(report, baseline_thresholds, "baseline")

        # Should have failures for: digests, errors, fill_rate (min), events_dropped
        assert len(failures) >= 4
