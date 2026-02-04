"""Idempotency utilities for connector write operations.

Provides idempotency guarantees for write operations (place/cancel/amend)
to prevent duplicate side-effects from retries or network issues.

Key concepts:
- IdempotencyKey: Deterministic hash of canonical payload (not UUID)
- IdempotencyStore: Pluggable storage for tracking operation status
- INFLIGHT/DONE/FAILED: Status states for idempotent operations

Usage:
    store = InMemoryIdempotencyStore()
    key = compute_idempotency_key("place", symbol="BTCUSDT", side="BUY", ...)

    # Check before executing
    entry = store.get(key)
    if entry and entry.status == IdempotencyStatus.DONE:
        return entry.result  # Return cached result

    # Mark as in-flight
    if not store.put_if_absent(key, IdempotencyEntry(...), ttl_s=300):
        raise IdempotencyConflictError(key)

    # Execute operation
    result = await do_operation()

    # Mark as done
    store.mark_done(key, result)

See: ADR-026 for design decisions
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol


class IdempotencyStatus(Enum):
    """Status of an idempotent operation."""

    INFLIGHT = "INFLIGHT"  # Operation in progress
    DONE = "DONE"  # Operation completed successfully
    FAILED = "FAILED"  # Operation failed (non-retryable)


@dataclass
class IdempotencyEntry:
    """Entry in the idempotency store.

    Attributes:
        key: Idempotency key
        status: Current status
        op_name: Operation name (e.g., "place", "cancel")
        request_fingerprint: Hash of canonical request (for sanity check)
        created_at: Unix timestamp when entry was created
        expires_at: Unix timestamp when entry expires
        result: Cached result (if DONE)
        error_code: Error code (if FAILED)
    """

    key: str
    status: IdempotencyStatus
    op_name: str
    request_fingerprint: str
    created_at: float
    expires_at: float
    result: Any | None = None
    error_code: str | None = None


@dataclass
class IdempotencyStats:
    """Statistics for idempotency store.

    Attributes:
        hits: Number of cache hits (DONE returned)
        misses: Number of cache misses (new operation)
        conflicts: Number of INFLIGHT conflicts
        expirations: Number of expired entries purged
        total_entries: Current number of entries
    """

    hits: int = 0
    misses: int = 0
    conflicts: int = 0
    expirations: int = 0
    total_entries: int = 0


class IdempotencyStore(Protocol):
    """Protocol for idempotency storage.

    Implementations must be thread-safe for concurrent access.
    """

    def get(self, key: str) -> IdempotencyEntry | None:
        """Get entry by key.

        Returns None if key not found or expired.
        """
        ...

    def put_if_absent(
        self,
        key: str,
        entry: IdempotencyEntry,
        ttl_s: float,
    ) -> bool:
        """Atomically put entry if key is absent or expired.

        Args:
            key: Idempotency key
            entry: Entry to store
            ttl_s: Time-to-live in seconds

        Returns:
            True if entry was stored, False if key already exists
        """
        ...

    def mark_done(self, key: str, result: Any) -> None:
        """Mark operation as done with result.

        Updates entry status to DONE and stores result.
        """
        ...

    def mark_failed(self, key: str, error_code: str) -> None:
        """Mark operation as failed with error code.

        Updates entry status to FAILED.
        """
        ...

    def purge_expired(self, now: float) -> int:
        """Remove expired entries.

        Args:
            now: Current time as Unix timestamp

        Returns:
            Number of entries purged
        """
        ...

    @property
    def stats(self) -> IdempotencyStats:
        """Get current statistics."""
        ...


@dataclass
class InMemoryIdempotencyStore:
    """In-memory implementation of IdempotencyStore.

    Thread-safe via lock. Suitable for:
    - Tests
    - Replay/paper mode
    - Single-process deployments

    For production multi-process deployments, use Redis implementation.
    """

    default_inflight_ttl_s: float = 300.0  # 5 minutes for INFLIGHT
    default_done_ttl_s: float = 86400.0  # 24 hours for DONE

    _entries: dict[str, IdempotencyEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stats: IdempotencyStats = field(default_factory=IdempotencyStats)

    # Injectable clock for testing
    _clock: Any = field(default=None)

    def __post_init__(self) -> None:
        """Initialize with defaults if needed."""
        if self._clock is None:
            self._clock = time

    def _now(self) -> float:
        """Get current time from clock."""
        return float(self._clock.time())

    def get(self, key: str) -> IdempotencyEntry | None:
        """Get entry by key.

        Returns None if key not found or expired.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None

            # Check expiration
            if entry.expires_at <= self._now():
                del self._entries[key]
                self._stats.expirations += 1
                self._stats.total_entries = len(self._entries)
                return None

            return entry

    def put_if_absent(
        self,
        key: str,
        entry: IdempotencyEntry,
        ttl_s: float,
    ) -> bool:
        """Atomically put entry if key is absent, expired, or FAILED.

        Returns True if entry was stored, False if key already exists and is active.
        FAILED entries can be overwritten to allow retry.
        """
        with self._lock:
            existing = self._entries.get(key)

            # Check if existing entry is still valid
            if existing is not None:
                if existing.expires_at > self._now():
                    # Key exists and not expired
                    # Allow overwrite of FAILED entries (retry allowed)
                    if existing.status == IdempotencyStatus.FAILED:
                        pass  # Allow overwrite
                    elif existing.status == IdempotencyStatus.INFLIGHT:
                        self._stats.conflicts += 1
                        return False
                    else:
                        # DONE - return hit
                        self._stats.hits += 1
                        return False
                else:
                    # Entry expired, remove it
                    self._stats.expirations += 1

            # Store new entry
            now = self._now()
            entry_with_expiry = IdempotencyEntry(
                key=entry.key,
                status=entry.status,
                op_name=entry.op_name,
                request_fingerprint=entry.request_fingerprint,
                created_at=now,
                expires_at=now + ttl_s,
                result=entry.result,
                error_code=entry.error_code,
            )
            self._entries[key] = entry_with_expiry
            self._stats.misses += 1
            self._stats.total_entries = len(self._entries)
            return True

    def mark_done(self, key: str, result: Any) -> None:
        """Mark operation as done with result."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return

            # Update entry with DONE status and new TTL
            now = self._now()
            self._entries[key] = IdempotencyEntry(
                key=entry.key,
                status=IdempotencyStatus.DONE,
                op_name=entry.op_name,
                request_fingerprint=entry.request_fingerprint,
                created_at=entry.created_at,
                expires_at=now + self.default_done_ttl_s,
                result=result,
                error_code=None,
            )

    def mark_failed(self, key: str, error_code: str) -> None:
        """Mark operation as failed with error code."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return

            # Update entry with FAILED status (keep shorter TTL)
            self._entries[key] = IdempotencyEntry(
                key=entry.key,
                status=IdempotencyStatus.FAILED,
                op_name=entry.op_name,
                request_fingerprint=entry.request_fingerprint,
                created_at=entry.created_at,
                expires_at=entry.expires_at,  # Keep original expiry
                result=None,
                error_code=error_code,
            )

    def purge_expired(self, now: float) -> int:
        """Remove expired entries."""
        with self._lock:
            expired_keys = [k for k, v in self._entries.items() if v.expires_at <= now]
            for key in expired_keys:
                del self._entries[key]
            self._stats.expirations += len(expired_keys)
            self._stats.total_entries = len(self._entries)
            return len(expired_keys)

    @property
    def stats(self) -> IdempotencyStats:
        """Get current statistics."""
        with self._lock:
            self._stats.total_entries = len(self._entries)
            return IdempotencyStats(
                hits=self._stats.hits,
                misses=self._stats.misses,
                conflicts=self._stats.conflicts,
                expirations=self._stats.expirations,
                total_entries=self._stats.total_entries,
            )

    def reset(self) -> None:
        """Reset store to initial state (for testing)."""
        with self._lock:
            self._entries.clear()
            self._stats = IdempotencyStats()


def _canonicalize_value(value: Any) -> str:
    """Convert value to canonical string representation."""
    if isinstance(value, Decimal):
        # Normalize Decimal to avoid trailing zeros affecting hash
        return str(value.normalize())
    if isinstance(value, float):
        # Convert float to Decimal for consistent representation
        return str(Decimal(str(value)).normalize())
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    return str(value)


def compute_idempotency_key(
    scope: str,
    op: str,
    *,
    symbol: str,
    side: str,
    price: Decimal | float | str | None = None,
    quantity: Decimal | float | str | None = None,
    order_id: str | None = None,
    level_id: int | None = None,
    **extra: Any,
) -> str:
    """Compute deterministic idempotency key from operation parameters.

    Key format: {scope}:{op}:{sha256_hex[:32]}

    The digest is computed from canonical JSON representation of parameters.
    Only parameters relevant to the operation are included.

    Args:
        scope: Scope identifier (e.g., "exec")
        op: Operation name (e.g., "place", "cancel", "amend")
        symbol: Trading symbol
        side: Order side (BUY/SELL)
        price: Limit price (optional for cancel)
        quantity: Order quantity (optional for cancel)
        order_id: Order ID (for cancel/amend)
        level_id: Grid level ID (optional)
        **extra: Additional parameters to include in hash

    Returns:
        Idempotency key in format "{scope}:{op}:{hash}"

    Example:
        >>> key = compute_idempotency_key(
        ...     "exec", "place",
        ...     symbol="BTCUSDT", side="BUY",
        ...     price=Decimal("50000.00"), quantity=Decimal("0.001"),
        ...     level_id=1
        ... )
        >>> key
        'exec:place:a1b2c3d4...'
    """
    # Build canonical payload (only non-None values)
    payload: dict[str, str] = {
        "symbol": symbol,
        "side": side,
    }

    if price is not None:
        payload["price"] = _canonicalize_value(price)
    if quantity is not None:
        payload["quantity"] = _canonicalize_value(quantity)
    if order_id is not None:
        payload["order_id"] = order_id
    if level_id is not None:
        payload["level_id"] = str(level_id)

    # Add extra parameters (sorted for determinism)
    for key, value in sorted(extra.items()):
        if value is not None:
            payload[key] = _canonicalize_value(value)

    # Compute deterministic JSON
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    # Hash with SHA-256
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    return f"{scope}:{op}:{digest}"


def compute_request_fingerprint(**params: Any) -> str:
    """Compute fingerprint of request parameters for sanity checking.

    This is a shorter hash used for detecting if two requests with the same
    idempotency key have different parameters (which would be a bug).

    Returns:
        16-character hex string
    """
    # Filter out None values and canonicalize
    filtered = {k: _canonicalize_value(v) for k, v in sorted(params.items()) if v is not None}
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
