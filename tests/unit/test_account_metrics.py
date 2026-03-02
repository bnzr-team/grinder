"""Tests for account sync metrics (Launch-15 PR1).

Validates:
- Prometheus export format correctness
- All 7 metrics from Spec 15.7 are present
- MetricsBuilder integration (patterns in REQUIRED_METRICS_PATTERNS)
- Singleton reset for test isolation

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.7)
"""

from grinder.account.metrics import (
    AccountSyncMetrics,
    get_account_sync_metrics,
    reset_account_sync_metrics,
)
from grinder.observability.metrics_builder import MetricsBuilder
from grinder.observability.metrics_contract import REQUIRED_METRICS_PATTERNS


class TestAccountSyncMetrics:
    def setup_method(self) -> None:
        reset_account_sync_metrics()

    def test_default_state(self) -> None:
        m = AccountSyncMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_last_ts 0" in text
        assert "grinder_account_sync_positions_count 0" in text
        assert "grinder_account_sync_open_orders_count 0" in text
        assert "grinder_account_sync_pending_notional 0.00" in text

    def test_record_sync(self) -> None:
        m = AccountSyncMetrics()
        m.record_sync(ts=1700000000000, positions=2, open_orders=5, pending_notional=12500.50)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_last_ts 1700000000000" in text
        assert "grinder_account_sync_positions_count 2" in text
        assert "grinder_account_sync_open_orders_count 5" in text
        assert "grinder_account_sync_pending_notional 12500.50" in text

    def test_empty_snapshot_still_updates_last_ts(self) -> None:
        """Empty account (ts=0) must not leave last_sync_ts at 0.

        Regression: build_account_snapshot() returns ts=0 for empty accounts.
        Metrics must still record a non-zero timestamp so age_seconds and
        alerting work correctly.
        """
        m = AccountSyncMetrics()
        m.record_sync(ts=0, positions=0, open_orders=0, pending_notional=0.0)
        assert m.last_sync_ts > 0, "last_sync_ts must be non-zero after successful sync"
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_last_ts 0" not in text

    def test_record_error(self) -> None:
        m = AccountSyncMetrics()
        m.record_error("timeout")
        m.record_error("timeout")
        m.record_error("auth")
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert 'grinder_account_sync_errors_total{reason="timeout"} 2' in text
        assert 'grinder_account_sync_errors_total{reason="auth"} 1' in text

    def test_record_mismatch(self) -> None:
        m = AccountSyncMetrics()
        m.record_mismatch("duplicate_key")
        m.record_mismatch("negative_qty")
        m.record_mismatch("duplicate_key")
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert 'grinder_account_sync_mismatches_total{rule="duplicate_key"} 2' in text
        assert 'grinder_account_sync_mismatches_total{rule="negative_qty"} 1' in text

    def test_default_errors_none_label(self) -> None:
        m = AccountSyncMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert 'grinder_account_sync_errors_total{reason="none"} 0' in text
        assert 'grinder_account_sync_mismatches_total{rule="none"} 0' in text

    def test_help_type_lines(self) -> None:
        m = AccountSyncMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "# HELP grinder_account_sync_last_ts" in text
        assert "# TYPE grinder_account_sync_last_ts gauge" in text
        assert "# HELP grinder_account_sync_age_seconds" in text
        assert "# TYPE grinder_account_sync_age_seconds gauge" in text
        assert "# HELP grinder_account_sync_errors_total" in text
        assert "# TYPE grinder_account_sync_errors_total counter" in text
        assert "# HELP grinder_account_sync_mismatches_total" in text
        assert "# TYPE grinder_account_sync_mismatches_total counter" in text
        assert "# HELP grinder_account_sync_positions_count" in text
        assert "# TYPE grinder_account_sync_positions_count gauge" in text
        assert "# HELP grinder_account_sync_open_orders_count" in text
        assert "# TYPE grinder_account_sync_open_orders_count gauge" in text
        assert "# HELP grinder_account_sync_pending_notional" in text
        assert "# TYPE grinder_account_sync_pending_notional gauge" in text


class TestAccountSyncMetricsSingleton:
    def setup_method(self) -> None:
        reset_account_sync_metrics()

    def test_singleton(self) -> None:
        m1 = get_account_sync_metrics()
        m2 = get_account_sync_metrics()
        assert m1 is m2

    def test_reset(self) -> None:
        m1 = get_account_sync_metrics()
        m1.record_error("test")
        reset_account_sync_metrics()
        m2 = get_account_sync_metrics()
        assert m2.sync_errors == {}


class TestAccountSyncMetricsContract:
    """Validates all account sync patterns appear in MetricsBuilder output."""

    def setup_method(self) -> None:
        reset_account_sync_metrics()

    def test_all_required_patterns_present(self) -> None:
        builder = MetricsBuilder()
        output = builder.build()
        account_patterns = [p for p in REQUIRED_METRICS_PATTERNS if "account_sync" in p]
        assert len(account_patterns) > 0, "No account_sync patterns in REQUIRED_METRICS_PATTERNS"
        for pattern in account_patterns:
            assert pattern in output, f"Missing pattern in MetricsBuilder output: {pattern}"
