"""Tests for LC-16 metrics contract smoke validation.

Validates that the smoke_metrics_contract.py script correctly:
1. Detects all required patterns when present
2. Reports missing patterns when absent
3. Detects forbidden labels when present
"""

from __future__ import annotations

import pytest

# Skip entire module if redis not installed (required by grinder.ha imports)
pytest.importorskip("redis", reason="redis not installed")

from scripts.smoke_metrics_contract import (
    check_forbidden_labels,
    validate_patterns,
)

from grinder.observability.live_contract import (
    FORBIDDEN_METRIC_LABELS,
    REQUIRED_METRICS_PATTERNS,
)


class TestPatternValidation:
    """Test required pattern validation."""

    def test_all_patterns_found_returns_empty_missing(self) -> None:
        """When all patterns are present, missing list should be empty."""
        # Build a metrics text containing all required patterns
        metrics_text = "\n".join(REQUIRED_METRICS_PATTERNS)

        missing, found = validate_patterns(metrics_text)

        assert len(missing) == 0
        assert len(found) == len(REQUIRED_METRICS_PATTERNS)

    def test_missing_pattern_detected(self) -> None:
        """When a pattern is missing, it should be in the missing list."""
        # Find a pattern that is NOT a substring of any other pattern
        # grinder_reconcile_last_remediation_ts_ms is unique
        unique_pattern = "grinder_reconcile_last_remediation_ts_ms"
        assert unique_pattern in REQUIRED_METRICS_PATTERNS

        # Build metrics text without this unique pattern
        patterns_except_unique = [
            p for p in REQUIRED_METRICS_PATTERNS if p != unique_pattern
        ]
        metrics_text = "\n".join(patterns_except_unique)

        missing, found = validate_patterns(metrics_text)

        assert unique_pattern in missing
        assert len(found) == len(REQUIRED_METRICS_PATTERNS) - 1

    def test_empty_metrics_all_patterns_missing(self) -> None:
        """Empty metrics should report all patterns as missing."""
        metrics_text = ""

        missing, found = validate_patterns(metrics_text)

        assert len(missing) == len(REQUIRED_METRICS_PATTERNS)
        assert len(found) == 0

    def test_partial_match_not_counted(self) -> None:
        """Partial pattern matches should not count as found."""
        # Pattern is "grinder_up 1", partial text should not match
        metrics_text = "grinder_up 0"  # Different value

        missing, _found = validate_patterns(metrics_text)

        # "grinder_up 1" should be missing since we have "grinder_up 0"
        assert "grinder_up 1" in missing


class TestForbiddenLabels:
    """Test forbidden label detection."""

    def test_no_forbidden_labels_returns_empty(self) -> None:
        """When no forbidden labels present, should return empty list."""
        metrics_text = """
# HELP grinder_up System up
# TYPE grinder_up gauge
grinder_up 1
grinder_connector_retries_total{op="cancel"} 5
"""

        found = check_forbidden_labels(metrics_text)

        assert len(found) == 0

    def test_forbidden_label_detected(self) -> None:
        """Forbidden labels should be detected."""
        # Include a forbidden label
        metrics_text = """
grinder_some_metric{symbol="BTCUSDT"} 1
"""

        found = check_forbidden_labels(metrics_text)

        assert "symbol=" in found

    def test_all_forbidden_labels_detected(self) -> None:
        """All types of forbidden labels should be detected."""
        # Build metrics with all forbidden labels
        metrics_parts = []
        for i, label in enumerate(FORBIDDEN_METRIC_LABELS):
            metrics_parts.append(f'grinder_metric_{i}{{{label}"value"}} 1')
        metrics_text = "\n".join(metrics_parts)

        found = check_forbidden_labels(metrics_text)

        assert len(found) == len(FORBIDDEN_METRIC_LABELS)


class TestExitCodes:
    """Test exit code contract."""

    def test_exit_codes_defined(self) -> None:
        """Verify exit codes are properly defined."""
        from scripts.smoke_metrics_contract import (  # noqa: PLC0415
            EXIT_CONNECTION_ERROR,
            EXIT_SUCCESS,
            EXIT_VALIDATION_FAILED,
        )

        assert EXIT_SUCCESS == 0
        assert EXIT_VALIDATION_FAILED == 1
        assert EXIT_CONNECTION_ERROR == 2


class TestContractCoverage:
    """Test that REQUIRED_METRICS_PATTERNS covers expected metrics."""

    def test_minimum_pattern_count(self) -> None:
        """Ensure we have a reasonable number of required patterns."""
        # Based on live_contract.py, we should have 60+ patterns
        assert len(REQUIRED_METRICS_PATTERNS) >= 50

    def test_help_type_patterns_present(self) -> None:
        """Ensure HELP and TYPE patterns are included."""
        help_patterns = [p for p in REQUIRED_METRICS_PATTERNS if p.startswith("# HELP")]
        type_patterns = [p for p in REQUIRED_METRICS_PATTERNS if p.startswith("# TYPE")]

        assert len(help_patterns) > 0
        assert len(type_patterns) > 0

    def test_series_patterns_present(self) -> None:
        """Ensure series-level patterns (with labels) are included."""
        series_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "{" in p]

        # H5-02 and LC patterns should include series-level checks
        assert len(series_patterns) > 0

    def test_reconcile_patterns_present(self) -> None:
        """Ensure reconcile-specific patterns are included."""
        reconcile_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "reconcile" in p]

        # LC-09b through LC-15b add multiple reconcile metrics
        assert len(reconcile_patterns) >= 10

    def test_forbidden_labels_reasonable(self) -> None:
        """Ensure forbidden labels list is reasonable."""
        # Should block high-cardinality labels
        assert len(FORBIDDEN_METRIC_LABELS) >= 3
        assert "symbol=" in FORBIDDEN_METRIC_LABELS
        assert "order_id=" in FORBIDDEN_METRIC_LABELS
