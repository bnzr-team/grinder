"""Periodic reconciliation loop for live trading.

Runs ReconcileRunner on a background thread with configurable interval.
Follows LeaderElector threading pattern (daemon thread, interruptible wait).

Safety guarantees:
- Default detect-only (action=NONE, dry_run=True)
- HA-aware: only runs when ACTIVE role
- Graceful shutdown via stop()
- Fail-safe: exceptions logged, loop continues

See ADR-048 for design decisions.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable  # noqa: TC003 - used at runtime in __init__
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from grinder.reconcile.runner import ReconcileRunner, ReconcileRunReport  # noqa: TC001

if TYPE_CHECKING:
    from grinder.ha.role import HARole

logger = logging.getLogger(__name__)

# Environment variable names
ENV_RECONCILE_ENABLED = "RECONCILE_ENABLED"
ENV_RECONCILE_INTERVAL_MS = "RECONCILE_INTERVAL_MS"

# Defaults
DEFAULT_INTERVAL_MS = 30_000  # 30 seconds
DEFAULT_ENABLED = False


def _get_bool_env(key: str, default: bool) -> bool:
    """Get boolean from environment variable (1/true/yes = True)."""
    value = os.environ.get(key, "").lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return default


def _get_int_env(key: str, default: int) -> int:
    """Get integer from environment variable."""
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class ReconcileLoopConfig:
    """Configuration for reconcile loop.

    Attributes:
        enabled: Whether reconcile loop is enabled (env: RECONCILE_ENABLED)
        interval_ms: How often to run reconciliation (env: RECONCILE_INTERVAL_MS)
        require_active_role: Only run when HA role is ACTIVE (default: True)
    """

    enabled: bool = field(
        default_factory=lambda: _get_bool_env(ENV_RECONCILE_ENABLED, DEFAULT_ENABLED)
    )
    interval_ms: int = field(
        default_factory=lambda: _get_int_env(ENV_RECONCILE_INTERVAL_MS, DEFAULT_INTERVAL_MS)
    )
    require_active_role: bool = True

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.interval_ms < 1000:
            msg = f"interval_ms ({self.interval_ms}) should be >= 1000ms"
            raise ValueError(msg)


@dataclass
class ReconcileLoopStats:
    """Statistics for reconcile loop.

    Attributes:
        runs_total: Total number of reconciliation runs
        runs_skipped_role: Runs skipped due to non-ACTIVE role
        runs_with_mismatch: Runs that found mismatches
        runs_with_error: Runs that had errors
        last_run_ts_ms: Timestamp of last run (0 if never)
        last_report: Last reconciliation report (None if never)
    """

    runs_total: int = 0
    runs_skipped_role: int = 0
    runs_with_mismatch: int = 0
    runs_with_error: int = 0
    last_run_ts_ms: int = 0
    last_report: ReconcileRunReport | None = None


class ReconcileLoop:
    """Periodic reconciliation runner.

    Runs ReconcileRunner on a background thread with configurable interval.
    Respects HA role (only runs when ACTIVE by default).

    Usage:
        runner = ReconcileRunner(...)
        loop = ReconcileLoop(runner, config)
        loop.start()  # Starts background thread
        ...
        loop.stop()   # Graceful shutdown

    Thread-safety: Safe to call start()/stop() from any thread.
    """

    def __init__(
        self,
        runner: ReconcileRunner,
        config: ReconcileLoopConfig | None = None,
        *,
        get_ha_role: Callable[[], HARole] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """Initialize reconcile loop.

        Args:
            runner: ReconcileRunner instance to execute
            config: Loop configuration (defaults from env vars)
            get_ha_role: Function to get current HA role (for testing)
            clock: Function returning current time in ms (for testing)
        """
        self._runner = runner
        self._config = config or ReconcileLoopConfig()
        self._get_ha_role = get_ha_role
        self._clock = clock or (lambda: int(time.time() * 1000))

        self._stop_event = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._is_running = False
        self._stats = ReconcileLoopStats()
        self._stats_lock = threading.Lock()

    @property
    def config(self) -> ReconcileLoopConfig:
        """Get current configuration."""
        return self._config

    @property
    def stats(self) -> ReconcileLoopStats:
        """Get current statistics (thread-safe copy)."""
        with self._stats_lock:
            return ReconcileLoopStats(
                runs_total=self._stats.runs_total,
                runs_skipped_role=self._stats.runs_skipped_role,
                runs_with_mismatch=self._stats.runs_with_mismatch,
                runs_with_error=self._stats.runs_with_error,
                last_run_ts_ms=self._stats.last_run_ts_ms,
                last_report=self._stats.last_report,
            )

    @property
    def is_running(self) -> bool:
        """Check if loop is currently running."""
        return self._is_running

    def start(self) -> None:
        """Start the reconciliation loop.

        If not enabled in config, logs info and returns without starting.
        If already running, returns immediately (idempotent).
        """
        if self._is_running:
            logger.debug("ReconcileLoop already running")
            return

        if not self._config.enabled:
            logger.info(
                "ReconcileLoop disabled",
                extra={"env": ENV_RECONCILE_ENABLED},
            )
            return

        self._stop_event.clear()
        self._loop_thread = threading.Thread(
            target=self._reconcile_loop,
            name="reconcile-loop",
            daemon=True,
        )
        self._loop_thread.start()
        self._is_running = True

        logger.info(
            "ReconcileLoop started",
            extra={
                "interval_ms": self._config.interval_ms,
                "require_active_role": self._config.require_active_role,
            },
        )

    def stop(self) -> None:
        """Stop the reconciliation loop.

        Signals the loop to stop and waits for thread to finish.
        Idempotent: safe to call multiple times.
        """
        if not self._is_running:
            return

        self._stop_event.set()
        if self._loop_thread:
            self._loop_thread.join(timeout=5.0)
            if self._loop_thread.is_alive():
                logger.warning("ReconcileLoop thread did not stop within timeout")

        self._is_running = False
        logger.info("ReconcileLoop stopped")

    def _reconcile_loop(self) -> None:
        """Run periodic reconciliation (internal loop)."""
        interval_s = self._config.interval_ms / 1000.0

        logger.debug(
            "Reconcile loop thread started",
            extra={"interval_s": interval_s},
        )

        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception:
                logger.exception("Error in reconcile loop")
                with self._stats_lock:
                    self._stats.runs_with_error += 1

            # Interruptible wait
            self._stop_event.wait(timeout=interval_s)

        logger.debug("Reconcile loop thread exiting")

    def _run_once(self) -> None:
        """Execute single reconciliation run."""
        # Check HA role if required
        if self._config.require_active_role:
            role = self._get_current_role()
            if role is not None and role.value != "ACTIVE":
                logger.debug(
                    "Skipping reconcile: not ACTIVE",
                    extra={"role": role.value},
                )
                with self._stats_lock:
                    self._stats.runs_skipped_role += 1
                return

        # Run reconciliation
        ts_start = self._clock()
        report = self._runner.run()
        ts_end = self._clock()

        # Update stats
        with self._stats_lock:
            self._stats.runs_total += 1
            self._stats.last_run_ts_ms = ts_end
            self._stats.last_report = report
            if report.mismatches_detected > 0:
                self._stats.runs_with_mismatch += 1

        logger.info(
            "RECONCILE_LOOP_RUN",
            extra={
                "duration_ms": ts_end - ts_start,
                "mismatches_detected": report.mismatches_detected,
                "planned_count": report.planned_count,
                "executed_count": report.executed_count,
                "blocked_count": report.blocked_count,
            },
        )

    def _get_current_role(self) -> HARole | None:
        """Get current HA role."""
        if self._get_ha_role is not None:
            return self._get_ha_role()

        # Try to import HA module (optional dependency)
        try:
            from grinder.ha.role import get_ha_state  # noqa: PLC0415

            return get_ha_state().role
        except ImportError:
            # HA module not available, assume ACTIVE
            return None
