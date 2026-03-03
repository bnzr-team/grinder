"""Tests for account sync metrics (Launch-15 PR1, PR-339).

Validates:
- Prometheus export format correctness
- All 9 metrics from Spec 15.7 are present (7 original + 2 PR-339)
- MetricsBuilder integration (patterns in REQUIRED_METRICS_PATTERNS)
- Singleton reset for test isolation
- PR-339: liveness (wall-clock) vs data freshness (exchange ts) semantics

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.7)
"""

from unittest.mock import patch

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
        assert "grinder_account_sync_last_wall_ts 0" in text
        assert "grinder_account_sync_positions_count 0" in text
        assert "grinder_account_sync_open_orders_count 0" in text
        assert "grinder_account_sync_pending_notional 0.00" in text

    def test_record_sync(self) -> None:
        m = AccountSyncMetrics()
        m.record_sync(ts=1700000000000, positions=2, open_orders=5, pending_notional=12500.50)
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_last_ts 1700000000000" in text
        assert m.last_wall_ts > 0
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
        assert "# HELP grinder_account_sync_last_wall_ts" in text
        assert "# TYPE grinder_account_sync_last_wall_ts gauge" in text
        assert "# HELP grinder_account_sync_age_seconds" in text
        assert "# TYPE grinder_account_sync_age_seconds gauge" in text
        assert "# HELP grinder_account_sync_data_age_seconds" in text
        assert "# TYPE grinder_account_sync_data_age_seconds gauge" in text
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


class TestWallClockLiveness:
    """PR-339: liveness (wall-clock) vs data freshness (exchange ts).

    Case A: unchanged open order — last_ts frozen, last_wall_ts advances, age low
    Case B: empty account — last_ts wall-clock fallback, last_wall_ts advances, age low
    Case C: normal updates — both last_ts and last_wall_ts advance
    """

    def setup_method(self) -> None:
        reset_account_sync_metrics()

    def test_case_a_unchanged_order_wall_ts_advances(self) -> None:
        """Case A: Repeated syncs with identical snapshot.ts (unchanged order).

        last_ts stays frozen at order creation time.
        last_wall_ts advances on every sync.
        age_seconds stays small (liveness).
        """
        m = AccountSyncMetrics()
        order_creation_ts = 1700000000000

        # First sync
        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000010.0  # 10s after order
            m.record_sync(ts=order_creation_ts, positions=0, open_orders=1, pending_notional=50.0)

        assert m.last_sync_ts == order_creation_ts
        wall_ts_1 = m.last_wall_ts
        assert wall_ts_1 == 1700000010000

        # Second sync 60s later — same order, same snapshot.ts
        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000070.0  # 70s after order
            m.record_sync(ts=order_creation_ts, positions=0, open_orders=1, pending_notional=50.0)

        assert m.last_sync_ts == order_creation_ts, "data_ts must NOT change"
        wall_ts_2 = m.last_wall_ts
        assert wall_ts_2 == 1700000070000
        assert wall_ts_2 > wall_ts_1, "wall_ts must advance"

        # age_seconds (liveness) should be small
        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000072.0  # 2s after last sync
            lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_age_seconds 2.00" in text

        # data_age_seconds should be large (72s since order creation)
        assert "grinder_account_sync_data_age_seconds 72.00" in text

    def test_case_b_empty_account_wall_ts_advances(self) -> None:
        """Case B: Empty account (ts=0).

        last_ts gets wall-clock fallback (existing behavior).
        last_wall_ts also set to wall-clock.
        age_seconds stays small.
        """
        m = AccountSyncMetrics()

        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000010.0
            m.record_sync(ts=0, positions=0, open_orders=0, pending_notional=0.0)

        assert m.last_sync_ts > 0, "empty-account fallback must set non-zero"
        assert m.last_wall_ts == 1700000010000

        # Second sync 30s later
        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000040.0
            m.record_sync(ts=0, positions=0, open_orders=0, pending_notional=0.0)

        assert m.last_wall_ts == 1700000040000

        # age_seconds should be small
        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000042.0
            lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_age_seconds 2.00" in text

    def test_case_c_normal_updates_both_advance(self) -> None:
        """Case C: Normal updates — both last_ts and last_wall_ts advance."""
        m = AccountSyncMetrics()

        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000010.0
            m.record_sync(ts=1700000009000, positions=1, open_orders=2, pending_notional=100.0)

        ts_1 = m.last_sync_ts
        wall_1 = m.last_wall_ts

        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000020.0
            m.record_sync(ts=1700000019000, positions=1, open_orders=2, pending_notional=100.0)

        assert m.last_sync_ts > ts_1, "data_ts must advance"
        assert m.last_wall_ts > wall_1, "wall_ts must advance"

    def test_age_zero_before_first_sync(self) -> None:
        """Before any sync, age_seconds and data_age_seconds are 0.0 (safe-by-default)."""
        m = AccountSyncMetrics()
        lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_age_seconds 0.00" in text
        assert "grinder_account_sync_data_age_seconds 0.00" in text

    def test_last_wall_ts_emitted_in_prometheus(self) -> None:
        """New last_wall_ts gauge appears in Prometheus output."""
        m = AccountSyncMetrics()
        with patch("grinder.account.metrics.time") as mock_time:
            mock_time.time.return_value = 1700000010.0
            m.record_sync(ts=1700000000000, positions=0, open_orders=1, pending_notional=50.0)
            lines = m.to_prometheus_lines()
        text = "\n".join(lines)
        assert "grinder_account_sync_last_wall_ts 1700000010000" in text
        assert "# HELP grinder_account_sync_last_wall_ts" in text
        assert "# TYPE grinder_account_sync_last_wall_ts gauge" in text
