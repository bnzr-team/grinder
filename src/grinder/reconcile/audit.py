"""Audit trail for reconciliation and remediation runs.

See ADR-046 for design decisions.

This module provides:
- AuditConfig: Configuration for audit logging
- AuditEventType: Event type enum (RECONCILE_RUN, REMEDIATE_ATTEMPT, REMEDIATE_RESULT)
- AuditEvent: Frozen dataclass for audit events
- AuditWriter: Append-only JSONL writer with bounded size

Key guarantees:
- Append-only writes (no overwrite)
- Bounded file size with rotation
- No secrets in output (redaction enabled by default)
- Deterministic serialization (sorted keys, no randomness)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Environment variable to enable audit
ENV_AUDIT_ENABLED = "GRINDER_AUDIT_ENABLED"
ENV_AUDIT_PATH = "GRINDER_AUDIT_PATH"

# Default values
DEFAULT_AUDIT_PATH = "audit/reconcile.jsonl"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_EVENTS_PER_FILE = 100_000

# Redaction patterns - fields that should never appear in audit
REDACTED_FIELDS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "password",
        "token",
        "signature",
        "x-mbx-apikey",
        "authorization",
    }
)


class AuditEventType(Enum):
    """Audit event types.

    These values are STABLE and used in audit files.
    DO NOT rename or remove values without migration.
    """

    RECONCILE_RUN = "RECONCILE_RUN"
    REMEDIATE_ATTEMPT = "REMEDIATE_ATTEMPT"
    REMEDIATE_RESULT = "REMEDIATE_RESULT"


@dataclass(frozen=True)
class AuditEvent:
    """Audit event for reconciliation/remediation.

    All fields are immutable and JSON-serializable.
    The schema is versioned via schema_version field.

    Attributes:
        ts_ms: Event timestamp in milliseconds
        event_type: Type of event
        run_id: Unique identifier for the reconcile run
        schema_version: Schema version for forward compatibility
        mode: Execution mode ("dry_run" or "live")
        action: Action type (NONE, CANCEL_ALL, FLATTEN)
        status: Event status (for REMEDIATE_* events)
        block_reason: Why blocked (if applicable)
        symbols: Symbols involved (bounded list)
        mismatch_counts: Counts by mismatch type
        details: Additional details (bounded dict)
    """

    ts_ms: int
    event_type: AuditEventType
    run_id: str
    schema_version: int = 1
    mode: str = "dry_run"
    action: str = "none"
    status: str | None = None
    block_reason: str | None = None
    symbols: tuple[str, ...] = ()
    mismatch_counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict with stable key ordering."""
        d = asdict(self)
        # Convert enum to string
        d["event_type"] = self.event_type.value
        # Convert tuple to list for JSON
        d["symbols"] = list(self.symbols)
        return d

    def to_json_line(self) -> str:
        """Serialize to JSON line (no trailing newline)."""
        return json.dumps(self.to_json_dict(), sort_keys=True, separators=(",", ":"))


@dataclass
class AuditConfig:
    """Configuration for audit logging.

    Attributes:
        enabled: Whether audit is enabled (default: False, opt-in)
        path: Path to audit JSONL file (default: audit/reconcile.jsonl)
        max_bytes: Max file size before rotation (default: 100MB)
        max_events_per_file: Max events per file (default: 100k)
        flush_every: Flush after N events (default: 1 = immediate)
        fsync: Call fsync after flush (default: False, for performance)
        redact: Enable redaction of sensitive fields (default: True)
        fail_open: Continue if write fails (default: True)
    """

    enabled: bool = False
    path: str = DEFAULT_AUDIT_PATH
    max_bytes: int = DEFAULT_MAX_BYTES
    max_events_per_file: int = DEFAULT_MAX_EVENTS_PER_FILE
    flush_every: int = 1
    fsync: bool = False
    redact: bool = True
    fail_open: bool = True

    def __post_init__(self) -> None:
        """Apply environment variable overrides."""
        if os.environ.get(ENV_AUDIT_ENABLED) == "1":
            object.__setattr__(self, "enabled", True)

        env_path = os.environ.get(ENV_AUDIT_PATH)
        if env_path:
            object.__setattr__(self, "path", env_path)


class AuditWriteError(Exception):
    """Error writing to audit file."""

    pass


@dataclass
class AuditWriter:
    """Append-only JSONL writer for audit events.

    Thread-safety: No (use separate instances per thread or external locking).

    Usage:
        writer = AuditWriter(AuditConfig(enabled=True))
        writer.write(event)
        writer.close()

    Or as context manager:
        with AuditWriter(config) as writer:
            writer.write(event)
    """

    config: AuditConfig
    _clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    _run_id_factory: Callable[[int], str] | None = None

    # Internal state
    _file: Any = field(default=None, init=False, repr=False)
    _event_count: int = field(default=0, init=False)
    _byte_count: int = field(default=0, init=False)
    _unflushed: int = field(default=0, init=False)
    _write_errors: int = field(default=0, init=False)
    _current_run_id: str | None = field(default=None, init=False)
    _run_seq: int = field(default=0, init=False)

    def __enter__(self) -> AuditWriter:
        """Enter context manager."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Exit context manager."""
        self.close()

    def _ensure_open(self) -> bool:
        """Ensure file is open, creating directories if needed.

        Returns:
            True if file is ready, False if disabled or error
        """
        if not self.config.enabled:
            return False

        if self._file is not None:
            return True

        try:
            path = Path(self.config.path)
            path.parent.mkdir(parents=True, exist_ok=True)

            # Get current size if file exists
            if path.exists():
                self._byte_count = path.stat().st_size

            # Open in append mode
            self._file = path.open("a", encoding="utf-8")
            return True

        except OSError as e:
            self._write_errors += 1
            logger.warning("AUDIT_OPEN_FAILED", extra={"error": str(e), "path": self.config.path})
            if not self.config.fail_open:
                raise AuditWriteError(f"Failed to open audit file: {e}") from e
            return False

    def _should_rotate(self) -> bool:
        """Check if rotation is needed."""
        return (
            self._byte_count >= self.config.max_bytes
            or self._event_count >= self.config.max_events_per_file
        )

    def _rotate(self) -> None:
        """Rotate audit file.

        Renames current file to .1, .2, etc.
        """
        if self._file is not None:
            self._file.close()
            self._file = None

        path = Path(self.config.path)
        if not path.exists():
            return

        # Find next rotation number
        n = 1
        while Path(f"{path}.{n}").exists():
            n += 1

        # Rename
        path.rename(f"{path}.{n}")
        logger.info(
            "AUDIT_ROTATED",
            extra={
                "old_path": str(path),
                "new_path": f"{path}.{n}",
                "events": self._event_count,
                "bytes": self._byte_count,
            },
        )

        # Reset counters
        self._event_count = 0
        self._byte_count = 0

    def generate_run_id(self) -> str:
        """Generate a unique run ID for a reconcile run.

        Format: {ts_ms}_{seq} for determinism in tests.
        Injectable via _run_id_factory for testing.
        """
        ts = self._clock()
        if self._run_id_factory:
            return self._run_id_factory(ts)

        self._run_seq += 1
        return f"{ts}_{self._run_seq}"

    def start_run(self) -> str:
        """Start a new reconcile run and return run_id."""
        self._current_run_id = self.generate_run_id()
        return self._current_run_id

    @property
    def current_run_id(self) -> str | None:
        """Get current run ID (None if no run started)."""
        return self._current_run_id

    def _redact_dict(self, d: dict[str, Any]) -> dict[str, Any]:
        """Recursively redact sensitive fields from dict."""
        if not self.config.redact:
            return d

        result: dict[str, Any] = {}
        for k, v in d.items():
            k_lower = k.lower()
            if k_lower in REDACTED_FIELDS or any(rf in k_lower for rf in REDACTED_FIELDS):
                result[k] = "[REDACTED]"
            elif isinstance(v, dict):
                result[k] = self._redact_dict(v)
            else:
                result[k] = v
        return result

    def write(self, event: AuditEvent) -> bool:
        """Write an audit event.

        Args:
            event: Event to write

        Returns:
            True if written successfully, False if disabled or failed

        Raises:
            AuditWriteError: If fail_open=False and write fails
        """
        if not self._ensure_open():
            return False

        # Check rotation before write
        if self._should_rotate():
            self._rotate()
            if not self._ensure_open():
                return False

        try:
            # Redact details if needed
            event_dict = event.to_json_dict()
            if self.config.redact and "details" in event_dict:
                event_dict["details"] = self._redact_dict(event_dict["details"])

            line = json.dumps(event_dict, sort_keys=True, separators=(",", ":"))
            line_bytes = line.encode("utf-8")

            self._file.write(line + "\n")
            self._event_count += 1
            self._byte_count += len(line_bytes) + 1  # +1 for newline
            self._unflushed += 1

            # Flush if needed
            if self._unflushed >= self.config.flush_every:
                self._file.flush()
                if self.config.fsync:
                    os.fsync(self._file.fileno())
                self._unflushed = 0

            return True

        except OSError as e:
            self._write_errors += 1
            logger.warning("AUDIT_WRITE_FAILED", extra={"error": str(e)})
            if not self.config.fail_open:
                raise AuditWriteError(f"Failed to write audit event: {e}") from e
            return False

    def flush(self) -> None:
        """Flush pending writes."""
        if self._file is not None and self._unflushed > 0:
            self._file.flush()
            if self.config.fsync:
                os.fsync(self._file.fileno())
            self._unflushed = 0

    def close(self) -> None:
        """Close the audit file."""
        if self._file is not None:
            self.flush()
            self._file.close()
            self._file = None

    @property
    def event_count(self) -> int:
        """Total events written in current file."""
        return self._event_count

    @property
    def byte_count(self) -> int:
        """Total bytes written in current file."""
        return self._byte_count

    @property
    def write_errors(self) -> int:
        """Total write errors encountered."""
        return self._write_errors


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================


def create_reconcile_run_event(
    run_id: str,
    ts_start: int,
    ts_end: int,
    mode: str,
    action: str,
    mismatch_counts: dict[str, int],
    symbols: list[str],
    cancel_count: int,
    flatten_count: int,
    executed_count: int,
    planned_count: int,
    blocked_count: int,
    skipped_terminal: int,
    skipped_no_action: int,
) -> AuditEvent:
    """Create a RECONCILE_RUN audit event.

    Args:
        run_id: Unique run identifier
        ts_start: Run start timestamp (ms)
        ts_end: Run end timestamp (ms)
        mode: "dry_run" or "live"
        action: Action type string
        mismatch_counts: Counts by mismatch type
        symbols: Symbols with mismatches
        cancel_count: Number of cancel actions
        flatten_count: Number of flatten actions
        executed_count: Actually executed
        planned_count: Dry-run planned
        blocked_count: Blocked by gates
        skipped_terminal: Skipped due to terminal status
        skipped_no_action: Skipped due to no action policy
    """
    return AuditEvent(
        ts_ms=ts_end,
        event_type=AuditEventType.RECONCILE_RUN,
        run_id=run_id,
        mode=mode,
        action=action,
        symbols=tuple(sorted(set(symbols))[:10]),  # Bounded, deterministic
        mismatch_counts=mismatch_counts,
        details={
            "ts_start": ts_start,
            "ts_end": ts_end,
            "duration_ms": ts_end - ts_start,
            "cancel_count": cancel_count,
            "flatten_count": flatten_count,
            "executed_count": executed_count,
            "planned_count": planned_count,
            "blocked_count": blocked_count,
            "skipped_terminal": skipped_terminal,
            "skipped_no_action": skipped_no_action,
        },
    )


def create_remediate_attempt_event(
    run_id: str,
    ts_ms: int,
    mode: str,
    action: str,
    symbol: str,
    client_order_id: str | None,
    mismatch_type: str,
) -> AuditEvent:
    """Create a REMEDIATE_ATTEMPT audit event.

    Args:
        run_id: Reconcile run identifier
        ts_ms: Attempt timestamp
        mode: "dry_run" or "live"
        action: "cancel_all" or "flatten"
        symbol: Trading symbol
        client_order_id: Order ID (for cancel) or None
        mismatch_type: Type of mismatch being remediated
    """
    return AuditEvent(
        ts_ms=ts_ms,
        event_type=AuditEventType.REMEDIATE_ATTEMPT,
        run_id=run_id,
        mode=mode,
        action=action,
        symbols=(symbol,),
        details={
            "client_order_id": client_order_id,
            "mismatch_type": mismatch_type,
        },
    )


def create_remediate_result_event(
    run_id: str,
    ts_ms: int,
    mode: str,
    action: str,
    symbol: str,
    client_order_id: str | None,
    mismatch_type: str,
    status: str,
    block_reason: str | None = None,
    error: str | None = None,
) -> AuditEvent:
    """Create a REMEDIATE_RESULT audit event.

    Args:
        run_id: Reconcile run identifier
        ts_ms: Result timestamp
        mode: "dry_run" or "live"
        action: "cancel_all" or "flatten"
        symbol: Trading symbol
        client_order_id: Order ID (for cancel) or None
        mismatch_type: Type of mismatch remediated
        status: Result status (PLANNED, EXECUTED, BLOCKED, FAILED)
        block_reason: Why blocked (if status=BLOCKED)
        error: Error message (if status=FAILED)
    """
    details: dict[str, Any] = {
        "client_order_id": client_order_id,
        "mismatch_type": mismatch_type,
    }
    if error:
        details["error"] = error

    return AuditEvent(
        ts_ms=ts_ms,
        event_type=AuditEventType.REMEDIATE_RESULT,
        run_id=run_id,
        mode=mode,
        action=action,
        status=status,
        block_reason=block_reason,
        symbols=(symbol,),
        details=details,
    )
