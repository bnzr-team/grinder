"""Artifact run-directory management (M4.1).

Provides structured artifact storage for reconciliation runs:
- Run-directory structure: $GRINDER_ARTIFACTS_DIR/YYYY-MM-DD/run_<ts>/
- Fixed filenames inside run-dir: stdout.log, audit.jsonl, metrics.prom, etc.
- Optional TTL cleanup for old run-dirs

Environment Variables:
    GRINDER_ARTIFACTS_DIR     Base directory for artifacts (enables run-dir mode)
    GRINDER_ARTIFACT_TTL_DAYS Days to keep old run-dirs (default: 14)

Backward Compatibility:
- If GRINDER_ARTIFACTS_DIR is not set, no run-dir is created
- If explicit --audit-out/--metrics-out are provided, they take precedence
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Environment variable names
ENV_ARTIFACTS_DIR = "GRINDER_ARTIFACTS_DIR"
ENV_ARTIFACT_TTL_DAYS = "GRINDER_ARTIFACT_TTL_DAYS"

# Fixed filenames inside run-dir
STDOUT_LOG = "stdout.log"
AUDIT_JSONL = "audit.jsonl"
METRICS_PROM = "metrics.prom"
METRICS_SUMMARY_JSON = "metrics_summary.json"
BUDGET_STATE_JSON = "budget_state.json"

# Default TTL
DEFAULT_TTL_DAYS = 14


@dataclass
class ArtifactPaths:
    """Resolved artifact paths for a reconcile run.

    Attributes:
        run_dir: Path to run directory (None if run-dir mode disabled)
        stdout_log: Path to stdout log file (None if disabled)
        audit_out: Path to audit JSONL file (None if disabled)
        metrics_out: Path to metrics Prometheus file (None if disabled)
        metrics_summary: Path to metrics summary JSON (None if disabled)
        budget_state: Path to budget state JSON snapshot (None if disabled)
    """

    run_dir: Path | None = None
    stdout_log: Path | None = None
    audit_out: Path | None = None
    metrics_out: Path | None = None
    metrics_summary: Path | None = None
    budget_state: Path | None = None

    # Track whether paths came from explicit CLI args
    audit_explicit: bool = False
    metrics_explicit: bool = False


@dataclass
class ArtifactConfig:
    """Configuration for artifact management.

    Attributes:
        base_dir: Base artifacts directory (from env or CLI)
        ttl_days: Days to keep old run-dirs (0 = no cleanup)
        explicit_audit_out: Explicit audit path from CLI (takes precedence)
        explicit_metrics_out: Explicit metrics path from CLI (takes precedence)
    """

    base_dir: Path | None = None
    ttl_days: int = DEFAULT_TTL_DAYS
    explicit_audit_out: str | None = None
    explicit_metrics_out: str | None = None


def load_artifact_config_from_env(
    explicit_audit_out: str | None = None,
    explicit_metrics_out: str | None = None,
) -> ArtifactConfig:
    """Load artifact configuration from environment variables.

    Args:
        explicit_audit_out: CLI-provided audit path (takes precedence)
        explicit_metrics_out: CLI-provided metrics path (takes precedence)

    Returns:
        ArtifactConfig with resolved settings
    """
    base_dir_str = os.environ.get(ENV_ARTIFACTS_DIR, "").strip()
    base_dir = Path(base_dir_str) if base_dir_str else None

    ttl_str = os.environ.get(ENV_ARTIFACT_TTL_DAYS, "").strip()
    try:
        ttl_days = int(ttl_str) if ttl_str else DEFAULT_TTL_DAYS
    except ValueError:
        logger.warning(
            f"Invalid {ENV_ARTIFACT_TTL_DAYS}={ttl_str}, using default {DEFAULT_TTL_DAYS}"
        )
        ttl_days = DEFAULT_TTL_DAYS

    return ArtifactConfig(
        base_dir=base_dir,
        ttl_days=ttl_days,
        explicit_audit_out=explicit_audit_out if explicit_audit_out else None,
        explicit_metrics_out=explicit_metrics_out if explicit_metrics_out else None,
    )


def resolve_artifact_paths(
    config: ArtifactConfig,
    run_ts_ms: int | None = None,
) -> ArtifactPaths:
    """Resolve artifact paths based on configuration.

    Rules:
    1. If config.base_dir is set and no explicit paths are given,
       create run-dir and use fixed filenames inside it.
    2. If explicit paths are given, use them directly (no run-dir).
    3. If neither, return all None (artifacts disabled).

    Args:
        config: Artifact configuration
        run_ts_ms: Run timestamp in milliseconds (default: now)

    Returns:
        ArtifactPaths with resolved paths
    """
    if run_ts_ms is None:
        run_ts_ms = int(datetime.now(UTC).timestamp() * 1000)

    # If both explicit paths are given, use them without run-dir
    if config.explicit_audit_out and config.explicit_metrics_out:
        return ArtifactPaths(
            run_dir=None,
            stdout_log=None,
            audit_out=Path(config.explicit_audit_out),
            metrics_out=Path(config.explicit_metrics_out),
            metrics_summary=None,
            budget_state=None,
            audit_explicit=True,
            metrics_explicit=True,
        )

    # If no base_dir, check for explicit paths
    if config.base_dir is None:
        audit = Path(config.explicit_audit_out) if config.explicit_audit_out else None
        metrics = Path(config.explicit_metrics_out) if config.explicit_metrics_out else None
        return ArtifactPaths(
            run_dir=None,
            stdout_log=None,
            audit_out=audit,
            metrics_out=metrics,
            metrics_summary=None,
            budget_state=None,
            audit_explicit=config.explicit_audit_out is not None,
            metrics_explicit=config.explicit_metrics_out is not None,
        )

    # Create run-dir structure
    date_str = datetime.fromtimestamp(run_ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
    run_dir = config.base_dir / date_str / f"run_{run_ts_ms}"

    return ArtifactPaths(
        run_dir=run_dir,
        stdout_log=run_dir / STDOUT_LOG,
        audit_out=Path(config.explicit_audit_out)
        if config.explicit_audit_out
        else run_dir / AUDIT_JSONL,
        metrics_out=Path(config.explicit_metrics_out)
        if config.explicit_metrics_out
        else run_dir / METRICS_PROM,
        metrics_summary=run_dir / METRICS_SUMMARY_JSON,
        budget_state=run_dir / BUDGET_STATE_JSON,
        audit_explicit=config.explicit_audit_out is not None,
        metrics_explicit=config.explicit_metrics_out is not None,
    )


def ensure_run_dir(paths: ArtifactPaths) -> bool:
    """Create run directory if needed.

    Args:
        paths: Resolved artifact paths

    Returns:
        True if run-dir was created or already exists, False if not using run-dir
    """
    if paths.run_dir is None:
        return False

    try:
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ARTIFACTS_RUN_DIR_CREATED path={paths.run_dir}")
        return True
    except OSError as e:
        logger.error(f"ARTIFACTS_RUN_DIR_CREATE_FAILED path={paths.run_dir} error={e}")
        return False


@dataclass
class TTLCleanupResult:
    """Result of TTL cleanup operation.

    Attributes:
        dirs_checked: Number of date directories checked
        dirs_deleted: Number of run directories deleted
        bytes_freed: Approximate bytes freed (0 if not tracked)
        oldest_date_deleted: Oldest date that was deleted (None if none)
        errors: List of error messages (empty if no errors)
    """

    dirs_checked: int = 0
    dirs_deleted: int = 0
    bytes_freed: int = 0
    oldest_date_deleted: str | None = None
    errors: list[str] = field(default_factory=list)


def cleanup_old_runs(
    base_dir: Path,
    ttl_days: int,
    dry_run: bool = False,
) -> TTLCleanupResult:
    """Remove run directories older than TTL.

    Scans $base_dir/YYYY-MM-DD/ directories and removes those
    where the date is older than (today - ttl_days).

    Args:
        base_dir: Base artifacts directory
        ttl_days: Days to keep (0 = no cleanup)
        dry_run: If True, don't actually delete, just report

    Returns:
        TTLCleanupResult with cleanup statistics
    """
    result = TTLCleanupResult()

    if ttl_days <= 0:
        logger.debug("TTL cleanup disabled (ttl_days=0)")
        return result

    if not base_dir.exists():
        logger.debug(f"TTL cleanup skipped: base_dir does not exist: {base_dir}")
        return result

    cutoff_date = datetime.now(UTC).date() - timedelta(days=ttl_days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")

    logger.info(
        f"ARTIFACTS_TTL_CLEANUP_START base_dir={base_dir} ttl_days={ttl_days} cutoff={cutoff_str}"
    )

    try:
        for date_dir in sorted(base_dir.iterdir()):
            if not date_dir.is_dir():
                continue

            # Check if directory name is a valid date
            date_name = date_dir.name
            try:
                dir_date = datetime.strptime(date_name, "%Y-%m-%d").date()
            except ValueError:
                # Not a date directory, skip
                continue

            result.dirs_checked += 1

            if dir_date < cutoff_date:
                # This directory is older than TTL
                if result.oldest_date_deleted is None or date_name < result.oldest_date_deleted:
                    result.oldest_date_deleted = date_name

                if dry_run:
                    logger.info(f"ARTIFACTS_TTL_WOULD_DELETE date_dir={date_dir}")
                    result.dirs_deleted += 1
                else:
                    try:
                        # Count files/subdirs for stats
                        run_count = sum(1 for _ in date_dir.iterdir() if _.is_dir())
                        shutil.rmtree(date_dir)
                        result.dirs_deleted += run_count
                        logger.info(f"ARTIFACTS_TTL_DELETED date_dir={date_dir} runs={run_count}")
                    except OSError as e:
                        result.errors.append(f"Failed to delete {date_dir}: {e}")
                        logger.error(f"ARTIFACTS_TTL_DELETE_FAILED date_dir={date_dir} error={e}")

    except OSError as e:
        result.errors.append(f"Failed to scan {base_dir}: {e}")
        logger.error(f"ARTIFACTS_TTL_SCAN_FAILED base_dir={base_dir} error={e}")

    logger.info(
        f"ARTIFACTS_TTL_CLEANUP_DONE dirs_checked={result.dirs_checked} "
        f"dirs_deleted={result.dirs_deleted} oldest={result.oldest_date_deleted}"
    )

    return result


def write_stdout_summary(
    path: Path,
    config_summary: dict[str, object],
    exit_code: int,
    paths: ArtifactPaths,
) -> bool:
    """Write stdout summary to file.

    This creates a summary file containing:
    - Config summary (mode, budgets, etc.)
    - Final exit code
    - Paths to other artifacts

    Args:
        path: Path to stdout.log
        config_summary: Dict of config values to record
        exit_code: Final exit code
        paths: Resolved artifact paths

    Returns:
        True if written successfully
    """
    try:
        lines = [
            "=" * 60,
            "  GRINDER RECONCILE RUN SUMMARY",
            "=" * 60,
            "",
            "Config:",
        ]

        for key, value in sorted(config_summary.items()):
            lines.append(f"  {key}: {value}")

        lines.extend(
            [
                "",
                "Artifact Paths:",
                f"  run_dir: {paths.run_dir}",
                f"  audit: {paths.audit_out}",
                f"  metrics: {paths.metrics_out}",
                f"  budget_state: {paths.budget_state}",
                "",
                "=" * 60,
                f"EXIT CODE: {exit_code}",
                "=" * 60,
            ]
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return True

    except OSError as e:
        logger.error(f"ARTIFACTS_STDOUT_WRITE_FAILED path={path} error={e}")
        return False


def copy_budget_state(
    source_path: str | None,
    dest_path: Path,
) -> bool:
    """Copy budget state file to run-dir.

    Args:
        source_path: Path to active budget state file (None = skip)
        dest_path: Destination path in run-dir

    Returns:
        True if copied successfully or source doesn't exist
    """
    if source_path is None:
        return True

    source = Path(source_path)
    if not source.exists():
        logger.debug(f"ARTIFACTS_BUDGET_STATE_NOT_FOUND source={source}")
        return True

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest_path)
        logger.debug(f"ARTIFACTS_BUDGET_STATE_COPIED source={source} dest={dest_path}")
        return True
    except OSError as e:
        logger.error(
            f"ARTIFACTS_BUDGET_STATE_COPY_FAILED source={source} dest={dest_path} error={e}"
        )
        return False
