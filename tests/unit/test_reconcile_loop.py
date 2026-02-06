"""Unit tests for ReconcileLoop (LC-14a).

Tests:
- ReconcileLoopConfig: defaults, env vars, validation
- ReconcileLoop: lifecycle, stats, HA integration
- Detect-only mode verification
"""

from __future__ import annotations

import os
import threading
import time
from enum import Enum
from unittest.mock import MagicMock, patch

import pytest

from grinder.live.reconcile_loop import (
    DEFAULT_ENABLED,
    DEFAULT_INTERVAL_MS,
    ENV_RECONCILE_ENABLED,
    ENV_RECONCILE_INTERVAL_MS,
    ReconcileLoop,
    ReconcileLoopConfig,
    ReconcileLoopStats,
)
from grinder.reconcile.runner import ReconcileRunReport

# =============================================================================
# Mock HA Role
# =============================================================================


class MockHARole(Enum):
    """Mock HA role for testing."""

    ACTIVE = "ACTIVE"
    STANDBY = "STANDBY"


# =============================================================================
# ReconcileLoopConfig Tests
# =============================================================================


class TestReconcileLoopConfig:
    """Tests for ReconcileLoopConfig."""

    def test_defaults(self) -> None:
        """Default config has sane values."""
        with patch.dict(os.environ, {}, clear=True):
            config = ReconcileLoopConfig()
            assert config.enabled == DEFAULT_ENABLED
            assert config.interval_ms == DEFAULT_INTERVAL_MS
            assert config.require_active_role is True

    def test_env_enabled(self) -> None:
        """RECONCILE_ENABLED=1 enables the loop."""
        with patch.dict(os.environ, {ENV_RECONCILE_ENABLED: "1"}):
            config = ReconcileLoopConfig()
            assert config.enabled is True

    def test_env_disabled(self) -> None:
        """RECONCILE_ENABLED=0 disables the loop."""
        with patch.dict(os.environ, {ENV_RECONCILE_ENABLED: "0"}):
            config = ReconcileLoopConfig()
            assert config.enabled is False

    def test_env_interval(self) -> None:
        """RECONCILE_INTERVAL_MS sets interval."""
        with patch.dict(os.environ, {ENV_RECONCILE_INTERVAL_MS: "5000"}):
            config = ReconcileLoopConfig()
            assert config.interval_ms == 5000

    def test_interval_validation(self) -> None:
        """Interval < 1000ms raises ValueError."""
        with pytest.raises(ValueError, match="interval_ms"):
            ReconcileLoopConfig(enabled=True, interval_ms=500)

    def test_explicit_values_override_env(self) -> None:
        """Explicit values override environment."""
        with patch.dict(
            os.environ, {ENV_RECONCILE_ENABLED: "1", ENV_RECONCILE_INTERVAL_MS: "5000"}
        ):
            config = ReconcileLoopConfig(enabled=False, interval_ms=10000)
            assert config.enabled is False
            assert config.interval_ms == 10000


# =============================================================================
# ReconcileLoop Lifecycle Tests
# =============================================================================


class TestReconcileLoopLifecycle:
    """Tests for ReconcileLoop start/stop lifecycle."""

    def test_start_when_disabled_does_nothing(self) -> None:
        """Start when disabled doesn't start thread."""
        mock_runner = MagicMock()
        config = ReconcileLoopConfig(enabled=False, interval_ms=1000)
        loop = ReconcileLoop(runner=mock_runner, config=config)

        loop.start()

        assert loop.is_running is False
        mock_runner.run.assert_not_called()

    def test_start_stop_lifecycle(self) -> None:
        """Start/stop lifecycle works correctly."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = self._make_report()

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,
        )
        loop = ReconcileLoop(runner=mock_runner, config=config)

        # Start
        loop.start()
        assert loop.is_running is True

        # Wait for at least one run
        time.sleep(0.1)

        # Stop
        loop.stop()
        assert loop.is_running is False

        # At least one run should have happened
        assert mock_runner.run.call_count >= 1

    def test_start_idempotent(self) -> None:
        """Multiple start() calls are safe."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = self._make_report()

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,
        )
        loop = ReconcileLoop(runner=mock_runner, config=config)

        loop.start()
        loop.start()  # Second call should be no-op
        loop.start()  # Third call should be no-op

        assert loop.is_running is True

        loop.stop()
        assert loop.is_running is False

    def test_stop_idempotent(self) -> None:
        """Multiple stop() calls are safe."""
        mock_runner = MagicMock()
        config = ReconcileLoopConfig(enabled=False, interval_ms=1000)
        loop = ReconcileLoop(runner=mock_runner, config=config)

        loop.stop()
        loop.stop()
        loop.stop()

        # Should not raise

    def _make_report(
        self,
        mismatches: int = 0,
    ) -> ReconcileRunReport:
        """Create mock report."""
        return ReconcileRunReport(
            ts_start=1000,
            ts_end=1010,
            mismatches_detected=mismatches,
            cancel_results=(),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )


# =============================================================================
# ReconcileLoop Stats Tests
# =============================================================================


class TestReconcileLoopStats:
    """Tests for ReconcileLoop statistics."""

    def test_stats_initial(self) -> None:
        """Initial stats are zero."""
        mock_runner = MagicMock()
        config = ReconcileLoopConfig(enabled=False, interval_ms=1000)
        loop = ReconcileLoop(runner=mock_runner, config=config)

        stats = loop.stats
        assert stats.runs_total == 0
        assert stats.runs_skipped_role == 0
        assert stats.runs_with_mismatch == 0
        assert stats.runs_with_error == 0
        assert stats.last_run_ts_ms == 0
        assert stats.last_report is None

    def test_stats_after_runs(self) -> None:
        """Stats are updated after runs."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = ReconcileRunReport(
            ts_start=1000,
            ts_end=1010,
            mismatches_detected=0,
            cancel_results=(),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,
        )
        loop = ReconcileLoop(
            runner=mock_runner,
            config=config,
            clock=lambda: 1704067200000,
        )

        loop.start()
        time.sleep(0.15)  # Wait for at least one run
        loop.stop()

        stats = loop.stats
        assert stats.runs_total >= 1
        assert stats.last_run_ts_ms == 1704067200000

    def test_stats_thread_safe(self) -> None:
        """Stats access is thread-safe."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = ReconcileRunReport(
            ts_start=1000,
            ts_end=1010,
            mismatches_detected=0,
            cancel_results=(),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,
        )
        loop = ReconcileLoop(runner=mock_runner, config=config)

        loop.start()

        # Access stats from multiple threads
        results: list[ReconcileLoopStats] = []

        def access_stats() -> None:
            for _ in range(10):
                results.append(loop.stats)
                time.sleep(0.01)

        threads = [threading.Thread(target=access_stats) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        loop.stop()

        # All accesses should succeed
        assert len(results) == 30


# =============================================================================
# HA Role Integration Tests
# =============================================================================


class TestReconcileLoopHAIntegration:
    """Tests for HA role integration."""

    def test_skip_when_not_active(self) -> None:
        """Skip reconciliation when not ACTIVE role."""
        mock_runner = MagicMock()

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=True,
        )

        # Return STANDBY role
        loop = ReconcileLoop(
            runner=mock_runner,
            config=config,
            get_ha_role=lambda: MockHARole.STANDBY,  # type: ignore[arg-type,return-value]
        )

        loop.start()
        time.sleep(0.15)
        loop.stop()

        # Should have skipped runs
        stats = loop.stats
        assert stats.runs_skipped_role >= 1
        assert stats.runs_total == 0  # No actual runs
        mock_runner.run.assert_not_called()

    def test_run_when_active(self) -> None:
        """Run reconciliation when ACTIVE role."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = ReconcileRunReport(
            ts_start=1000,
            ts_end=1010,
            mismatches_detected=0,
            cancel_results=(),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=True,
        )

        # Return ACTIVE role
        loop = ReconcileLoop(
            runner=mock_runner,
            config=config,
            get_ha_role=lambda: MockHARole.ACTIVE,  # type: ignore[arg-type,return-value]
        )

        loop.start()
        time.sleep(0.15)
        loop.stop()

        # Should have run
        stats = loop.stats
        assert stats.runs_total >= 1
        assert stats.runs_skipped_role == 0
        assert mock_runner.run.call_count >= 1

    def test_require_active_role_false_always_runs(self) -> None:
        """When require_active_role=False, always runs."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = ReconcileRunReport(
            ts_start=1000,
            ts_end=1010,
            mismatches_detected=0,
            cancel_results=(),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,  # Don't check role
        )

        loop = ReconcileLoop(
            runner=mock_runner,
            config=config,
            get_ha_role=lambda: MockHARole.STANDBY,  # type: ignore[arg-type,return-value]
        )

        loop.start()
        time.sleep(0.15)
        loop.stop()

        # Should have run despite STANDBY role
        stats = loop.stats
        assert stats.runs_total >= 1
        assert mock_runner.run.call_count >= 1


# =============================================================================
# Detect-Only Mode Tests
# =============================================================================


class TestReconcileLoopDetectOnly:
    """Tests for detect-only mode behavior."""

    def test_detect_only_records_mismatches(self) -> None:
        """Detect-only mode records mismatches without execution."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = ReconcileRunReport(
            ts_start=1000,
            ts_end=1010,
            mismatches_detected=5,  # Some mismatches
            cancel_results=(),
            flatten_results=(),
            skipped_terminal=0,
            skipped_no_action=0,
        )

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,
        )

        loop = ReconcileLoop(runner=mock_runner, config=config)

        loop.start()
        time.sleep(0.15)
        loop.stop()

        stats = loop.stats
        assert stats.runs_with_mismatch >= 1
        assert stats.last_report is not None
        assert stats.last_report.mismatches_detected == 5


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestReconcileLoopErrorHandling:
    """Tests for error handling."""

    def test_runner_exception_continues_loop(self) -> None:
        """Exception in runner doesn't stop the loop."""
        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("Simulated error")

        config = ReconcileLoopConfig(
            enabled=True,
            interval_ms=1000,
            require_active_role=False,
        )

        loop = ReconcileLoop(runner=mock_runner, config=config)

        loop.start()
        # Wait for at least one run (runs immediately on start)
        time.sleep(0.15)
        loop.stop()

        # Loop should still be functional after error
        stats = loop.stats
        assert stats.runs_with_error >= 1
        # Runner should have been called at least once
        assert mock_runner.run.call_count >= 1
