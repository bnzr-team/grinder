"""Unit tests for artifacts run-dir management (M4.1).

Tests:
- resolve_artifact_paths() path resolution logic
- ensure_run_dir() directory creation
- cleanup_old_runs() TTL cleanup
- write_stdout_summary() summary file writing
- copy_budget_state() budget state copying
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest  # noqa: TC002

from grinder.ops.artifacts import (
    AUDIT_JSONL,
    BUDGET_STATE_JSON,
    METRICS_PROM,
    METRICS_SUMMARY_JSON,
    STDOUT_LOG,
    ArtifactConfig,
    ArtifactPaths,
    cleanup_old_runs,
    copy_budget_state,
    ensure_run_dir,
    load_artifact_config_from_env,
    resolve_artifact_paths,
    write_stdout_summary,
)


class TestLoadArtifactConfigFromEnv:
    """Tests for load_artifact_config_from_env()."""

    def test_no_env_vars_returns_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no env vars are set, returns defaults."""
        monkeypatch.delenv("GRINDER_ARTIFACTS_DIR", raising=False)
        monkeypatch.delenv("GRINDER_ARTIFACT_TTL_DAYS", raising=False)

        config = load_artifact_config_from_env()

        assert config.base_dir is None
        assert config.ttl_days == 14
        assert config.explicit_audit_out is None
        assert config.explicit_metrics_out is None

    def test_artifacts_dir_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_ARTIFACTS_DIR sets base_dir."""
        monkeypatch.setenv("GRINDER_ARTIFACTS_DIR", "/var/log/grinder")

        config = load_artifact_config_from_env()

        assert config.base_dir == Path("/var/log/grinder")

    def test_ttl_days_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GRINDER_ARTIFACT_TTL_DAYS sets ttl_days."""
        monkeypatch.setenv("GRINDER_ARTIFACT_TTL_DAYS", "7")

        config = load_artifact_config_from_env()

        assert config.ttl_days == 7

    def test_invalid_ttl_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid GRINDER_ARTIFACT_TTL_DAYS uses default."""
        monkeypatch.setenv("GRINDER_ARTIFACT_TTL_DAYS", "invalid")

        config = load_artifact_config_from_env()

        assert config.ttl_days == 14

    def test_explicit_paths_passed_through(self) -> None:
        """Explicit audit/metrics paths are passed through."""
        config = load_artifact_config_from_env(
            explicit_audit_out="/tmp/audit.jsonl",
            explicit_metrics_out="/tmp/metrics.prom",
        )

        assert config.explicit_audit_out == "/tmp/audit.jsonl"
        assert config.explicit_metrics_out == "/tmp/metrics.prom"


class TestResolveArtifactPaths:
    """Tests for resolve_artifact_paths()."""

    def test_no_config_returns_all_none(self) -> None:
        """When no base_dir or explicit paths, returns all None."""
        config = ArtifactConfig()

        paths = resolve_artifact_paths(config)

        assert paths.run_dir is None
        assert paths.stdout_log is None
        assert paths.audit_out is None
        assert paths.metrics_out is None

    def test_explicit_paths_only(self) -> None:
        """When only explicit paths are given, no run-dir is created."""
        config = ArtifactConfig(
            explicit_audit_out="/tmp/audit.jsonl",
            explicit_metrics_out="/tmp/metrics.prom",
        )

        paths = resolve_artifact_paths(config)

        assert paths.run_dir is None
        assert paths.stdout_log is None
        assert paths.audit_out == Path("/tmp/audit.jsonl")
        assert paths.metrics_out == Path("/tmp/metrics.prom")
        assert paths.audit_explicit is True
        assert paths.metrics_explicit is True

    def test_base_dir_creates_run_dir(self, tmp_path: Path) -> None:
        """When base_dir is set, creates run-dir structure."""
        config = ArtifactConfig(base_dir=tmp_path)
        ts_ms = 1707307200000  # 2024-02-07 12:00:00 UTC

        paths = resolve_artifact_paths(config, run_ts_ms=ts_ms)

        assert paths.run_dir is not None
        assert "2024-02-07" in str(paths.run_dir)
        assert f"run_{ts_ms}" in str(paths.run_dir)
        assert paths.stdout_log == paths.run_dir / STDOUT_LOG
        assert paths.audit_out == paths.run_dir / AUDIT_JSONL
        assert paths.metrics_out == paths.run_dir / METRICS_PROM
        assert paths.metrics_summary == paths.run_dir / METRICS_SUMMARY_JSON
        assert paths.budget_state == paths.run_dir / BUDGET_STATE_JSON

    def test_base_dir_with_explicit_audit_uses_explicit(self, tmp_path: Path) -> None:
        """When base_dir + explicit audit, audit uses explicit path."""
        config = ArtifactConfig(
            base_dir=tmp_path,
            explicit_audit_out="/tmp/explicit_audit.jsonl",
        )

        paths = resolve_artifact_paths(config)

        assert paths.run_dir is not None
        assert paths.audit_out == Path("/tmp/explicit_audit.jsonl")
        assert paths.audit_explicit is True
        assert paths.metrics_out == paths.run_dir / METRICS_PROM
        assert paths.metrics_explicit is False


class TestEnsureRunDir:
    """Tests for ensure_run_dir()."""

    def test_no_run_dir_returns_false(self) -> None:
        """When run_dir is None, returns False."""
        paths = ArtifactPaths()

        result = ensure_run_dir(paths)

        assert result is False

    def test_creates_run_dir(self, tmp_path: Path) -> None:
        """Creates run directory when specified."""
        run_dir = tmp_path / "2024-02-07" / "run_123"
        paths = ArtifactPaths(run_dir=run_dir)

        result = ensure_run_dir(paths)

        assert result is True
        assert run_dir.exists()
        assert run_dir.is_dir()

    def test_existing_run_dir_ok(self, tmp_path: Path) -> None:
        """Existing run directory is not an error."""
        run_dir = tmp_path / "2024-02-07" / "run_123"
        run_dir.mkdir(parents=True)
        paths = ArtifactPaths(run_dir=run_dir)

        result = ensure_run_dir(paths)

        assert result is True


class TestCleanupOldRuns:
    """Tests for cleanup_old_runs()."""

    def test_ttl_zero_skips_cleanup(self, tmp_path: Path) -> None:
        """TTL of 0 skips cleanup."""
        result = cleanup_old_runs(tmp_path, ttl_days=0)

        assert result.dirs_checked == 0
        assert result.dirs_deleted == 0

    def test_nonexistent_base_dir_skips(self, tmp_path: Path) -> None:
        """Non-existent base_dir skips cleanup."""
        result = cleanup_old_runs(tmp_path / "nonexistent", ttl_days=14)

        assert result.dirs_checked == 0

    def test_deletes_old_date_dirs(self, tmp_path: Path) -> None:
        """Deletes date directories older than TTL."""
        # Create old directory (20 days ago)
        old_date = (datetime.now(UTC) - timedelta(days=20)).strftime("%Y-%m-%d")
        old_dir = tmp_path / old_date
        old_dir.mkdir()
        (old_dir / "run_1").mkdir()
        (old_dir / "run_2").mkdir()

        # Create recent directory (5 days ago)
        recent_date = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%d")
        recent_dir = tmp_path / recent_date
        recent_dir.mkdir()
        (recent_dir / "run_1").mkdir()

        result = cleanup_old_runs(tmp_path, ttl_days=14)

        assert result.dirs_checked == 2
        assert result.dirs_deleted == 2  # 2 run dirs in old_date
        assert not old_dir.exists()
        assert recent_dir.exists()
        assert result.oldest_date_deleted == old_date

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        """Dry run reports but does not delete."""
        old_date = (datetime.now(UTC) - timedelta(days=20)).strftime("%Y-%m-%d")
        old_dir = tmp_path / old_date
        old_dir.mkdir()
        (old_dir / "run_1").mkdir()

        result = cleanup_old_runs(tmp_path, ttl_days=14, dry_run=True)

        assert result.dirs_deleted == 1  # Would delete
        assert old_dir.exists()  # But didn't

    def test_ignores_non_date_directories(self, tmp_path: Path) -> None:
        """Ignores directories that aren't date-formatted."""
        (tmp_path / "not-a-date").mkdir()
        (tmp_path / "config").mkdir()

        result = cleanup_old_runs(tmp_path, ttl_days=14)

        assert result.dirs_checked == 0
        assert (tmp_path / "not-a-date").exists()
        assert (tmp_path / "config").exists()


class TestWriteStdoutSummary:
    """Tests for write_stdout_summary()."""

    def test_writes_summary_file(self, tmp_path: Path) -> None:
        """Writes summary file with config and paths."""
        path = tmp_path / "stdout.log"
        run_dir = tmp_path / "run_123"
        paths = ArtifactPaths(
            run_dir=run_dir,
            stdout_log=path,
            audit_out=run_dir / "audit.jsonl",
            metrics_out=run_dir / "metrics.prom",
            budget_state=run_dir / "budget_state.json",
        )
        config_summary = {
            "mode": "detect_only",
            "duration": 60,
        }

        result = write_stdout_summary(path, config_summary, exit_code=0, paths=paths)

        assert result is True
        assert path.exists()
        content = path.read_text()
        assert "GRINDER RECONCILE RUN SUMMARY" in content
        assert "mode: detect_only" in content
        assert "duration: 60" in content
        assert "EXIT CODE: 0" in content


class TestCopyBudgetState:
    """Tests for copy_budget_state()."""

    def test_none_source_returns_true(self, tmp_path: Path) -> None:
        """None source path returns True (no-op)."""
        dest = tmp_path / "budget_state.json"

        result = copy_budget_state(None, dest)

        assert result is True
        assert not dest.exists()

    def test_nonexistent_source_returns_true(self, tmp_path: Path) -> None:
        """Non-existent source returns True."""
        dest = tmp_path / "budget_state.json"

        result = copy_budget_state("/nonexistent/path.json", dest)

        assert result is True
        assert not dest.exists()

    def test_copies_existing_file(self, tmp_path: Path) -> None:
        """Copies existing budget state file."""
        source = tmp_path / "source" / "budget.json"
        source.parent.mkdir()
        source.write_text('{"date": "2024-02-07", "calls_today": 5}')

        dest = tmp_path / "dest" / "budget_state.json"

        result = copy_budget_state(str(source), dest)

        assert result is True
        assert dest.exists()
        assert dest.read_text() == source.read_text()


class TestIntegration:
    """Integration tests for the full artifact flow."""

    def test_full_artifact_flow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test full flow: config -> resolve -> create -> write."""
        # Set up environment
        monkeypatch.setenv("GRINDER_ARTIFACTS_DIR", str(tmp_path))
        monkeypatch.setenv("GRINDER_ARTIFACT_TTL_DAYS", "7")

        # Load config
        config = load_artifact_config_from_env()
        assert config.base_dir == tmp_path
        assert config.ttl_days == 7

        # Resolve paths
        ts_ms = int(datetime.now(UTC).timestamp() * 1000)
        paths = resolve_artifact_paths(config, run_ts_ms=ts_ms)
        assert paths.run_dir is not None

        # Create run-dir
        result = ensure_run_dir(paths)
        assert result is True
        assert paths.run_dir.exists()

        # Write summary
        write_stdout_summary(
            path=paths.stdout_log,
            config_summary={"mode": "detect_only"},
            exit_code=0,
            paths=paths,
        )
        assert paths.stdout_log.exists()

        # Verify structure
        assert (paths.run_dir.parent.name).startswith("20")  # Date directory
        assert paths.run_dir.name.startswith("run_")
