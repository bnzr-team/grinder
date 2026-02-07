"""Unit tests for budget state lifecycle management (M4.2).

Tests:
- reset_budget_state() file deletion
- check_budget_state_stale() warning detection
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest  # noqa: TC002

from grinder.ops.budget import (
    DEFAULT_STALE_HOURS,
    check_budget_state_stale,
    reset_budget_state,
)


class TestResetBudgetState:
    """Tests for reset_budget_state()."""

    def test_none_path_returns_true(self) -> None:
        """None path returns True (no-op)."""
        result = reset_budget_state(None)
        assert result is True

    def test_nonexistent_file_returns_true(self, tmp_path: Path) -> None:
        """Non-existent file returns True."""
        path = tmp_path / "nonexistent.json"
        result = reset_budget_state(str(path))
        assert result is True
        assert not path.exists()

    def test_deletes_existing_file(self, tmp_path: Path) -> None:
        """Existing file is deleted."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15", "calls_today": 5}')
        assert path.exists()

        result = reset_budget_state(str(path))

        assert result is True
        assert not path.exists()

    def test_returns_false_on_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when file deletion fails."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        # Mock unlink to raise OSError
        def mock_unlink(self: Path) -> None:
            raise OSError("Permission denied")

        monkeypatch.setattr(Path, "unlink", mock_unlink)

        result = reset_budget_state(str(path))

        assert result is False


class TestCheckBudgetStateStale:
    """Tests for check_budget_state_stale()."""

    def test_none_path_returns_false(self) -> None:
        """None path returns False (no-op)."""
        result = check_budget_state_stale(None)
        assert result is False

    def test_nonexistent_file_returns_false(self, tmp_path: Path) -> None:
        """Non-existent file returns False."""
        path = tmp_path / "nonexistent.json"
        result = check_budget_state_stale(str(path))
        assert result is False

    def test_fresh_file_returns_false(self, tmp_path: Path) -> None:
        """Fresh file (just created) returns False."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        result = check_budget_state_stale(str(path))

        assert result is False

    def test_stale_file_returns_true(self, tmp_path: Path) -> None:
        """Stale file (older than threshold) returns True."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        # Set mtime to 25 hours ago
        stale_time = datetime.now(UTC) - timedelta(hours=25)
        stale_ts = stale_time.timestamp()
        os.utime(path, (stale_ts, stale_ts))

        result = check_budget_state_stale(str(path))

        assert result is True

    def test_custom_threshold(self, tmp_path: Path) -> None:
        """Custom threshold is respected."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        # Set mtime to 2 hours ago
        stale_time = datetime.now(UTC) - timedelta(hours=2)
        stale_ts = stale_time.timestamp()
        os.utime(path, (stale_ts, stale_ts))

        # With 1 hour threshold, should be stale
        result_stale = check_budget_state_stale(str(path), stale_hours=1)
        assert result_stale is True

        # With 24 hour threshold, should not be stale
        result_fresh = check_budget_state_stale(str(path), stale_hours=24)
        assert result_fresh is False

    def test_env_var_threshold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable threshold is respected."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        # Set mtime to 2 hours ago
        stale_time = datetime.now(UTC) - timedelta(hours=2)
        stale_ts = stale_time.timestamp()
        os.utime(path, (stale_ts, stale_ts))

        # Set env var to 1 hour
        monkeypatch.setenv("BUDGET_STATE_STALE_HOURS", "1")

        result = check_budget_state_stale(str(path))

        assert result is True

    def test_invalid_env_var_uses_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid env var uses default threshold."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        monkeypatch.setenv("BUDGET_STATE_STALE_HOURS", "invalid")

        # Should use default (24h) - fresh file should not be stale
        result = check_budget_state_stale(str(path))

        assert result is False

    def test_default_stale_hours_is_24(self) -> None:
        """Default stale threshold is 24 hours."""
        assert DEFAULT_STALE_HOURS == 24


class TestBudgetLifecycleIntegration:
    """Integration tests for budget lifecycle flow."""

    def test_reset_then_check_stale(self, tmp_path: Path) -> None:
        """After reset, stale check should return False (no file)."""
        path = tmp_path / "budget_state.json"
        path.write_text('{"date": "2024-01-15"}')

        # Reset
        reset_result = reset_budget_state(str(path))
        assert reset_result is True

        # Check stale - file gone, should return False
        stale_result = check_budget_state_stale(str(path))
        assert stale_result is False

    def test_multi_run_budget_persists(self, tmp_path: Path) -> None:
        """Without reset, budget state file persists between runs."""
        path = tmp_path / "budget_state.json"
        content = '{"date": "2024-01-15", "calls_today": 5}'
        path.write_text(content)

        # Simulate "run" without reset - file should still exist
        # (just check stale, don't reset)
        check_budget_state_stale(str(path))

        # File should still exist with same content
        assert path.exists()
        assert path.read_text() == content
