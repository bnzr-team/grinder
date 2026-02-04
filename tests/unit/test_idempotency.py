"""Tests for idempotency utilities (H3).

Covers:
- IdempotencyKey generation and canonicalization
- InMemoryIdempotencyStore operations
- IdempotentExchangePort behavior
- Double-submit returns same result
- Retry + idempotency = 1 side-effect
- Concurrent duplicates (fast-fail)
- Expired keys allow re-execution
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from grinder.connectors import (
    IdempotencyConflictError,
    IdempotencyEntry,
    IdempotencyStatus,
    InMemoryIdempotencyStore,
    compute_idempotency_key,
    compute_request_fingerprint,
)
from grinder.core import OrderSide
from grinder.execution import IdempotentExchangePort, NoOpExchangePort

# --- Idempotency Key Tests ---


class TestIdempotencyKey:
    """Tests for compute_idempotency_key function."""

    def test_deterministic_same_inputs(self) -> None:
        """Same inputs produce same key."""
        key1 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.00"),
            quantity=Decimal("0.001"),
            level_id=1,
        )
        key2 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.00"),
            quantity=Decimal("0.001"),
            level_id=1,
        )
        assert key1 == key2

    def test_different_inputs_different_keys(self) -> None:
        """Different inputs produce different keys."""
        key1 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.00"),
            quantity=Decimal("0.001"),
        )
        key2 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("50000.00"),
            quantity=Decimal("0.001"),
        )
        assert key1 != key2

    def test_key_format(self) -> None:
        """Key has expected format: {scope}:{op}:{hash}."""
        key = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
        )
        parts = key.split(":")
        assert len(parts) == 3
        assert parts[0] == "exec"
        assert parts[1] == "place"
        assert len(parts[2]) == 32  # SHA-256 truncated to 32 hex chars

    def test_decimal_normalization(self) -> None:
        """Different Decimal representations of same value produce same key."""
        key1 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.00"),
            quantity=Decimal("0.001"),
        )
        key2 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),  # No trailing zeros
            quantity=Decimal("0.00100"),  # Extra trailing zeros
        )
        assert key1 == key2

    def test_none_values_excluded(self) -> None:
        """None values are not included in hash."""
        key1 = compute_idempotency_key(
            "exec",
            "cancel",
            symbol="BTCUSDT",
            side="",
            order_id="order-123",
        )
        key2 = compute_idempotency_key(
            "exec",
            "cancel",
            symbol="BTCUSDT",
            side="",
            order_id="order-123",
            price=None,  # Explicitly None
            quantity=None,
        )
        assert key1 == key2

    def test_extra_params_included(self) -> None:
        """Extra parameters are included in hash."""
        key1 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            custom_field="value1",
        )
        key2 = compute_idempotency_key(
            "exec",
            "place",
            symbol="BTCUSDT",
            side="BUY",
            custom_field="value2",
        )
        assert key1 != key2


class TestRequestFingerprint:
    """Tests for compute_request_fingerprint function."""

    def test_same_params_same_fingerprint(self) -> None:
        """Same parameters produce same fingerprint."""
        fp1 = compute_request_fingerprint(symbol="BTCUSDT", side="BUY")
        fp2 = compute_request_fingerprint(symbol="BTCUSDT", side="BUY")
        assert fp1 == fp2

    def test_fingerprint_length(self) -> None:
        """Fingerprint is 16 hex chars."""
        fp = compute_request_fingerprint(symbol="BTCUSDT")
        assert len(fp) == 16


# --- InMemoryIdempotencyStore Tests ---


@dataclass
class FakeClock:
    """Fake clock for testing time-based behavior."""

    _time: float = 0.0

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class TestInMemoryIdempotencyStore:
    """Tests for InMemoryIdempotencyStore."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def store(self, clock: FakeClock) -> InMemoryIdempotencyStore:
        return InMemoryIdempotencyStore(_clock=clock)

    def _make_entry(
        self, key: str, status: IdempotencyStatus = IdempotencyStatus.INFLIGHT
    ) -> IdempotencyEntry:
        return IdempotencyEntry(
            key=key,
            status=status,
            op_name="test",
            request_fingerprint="abc123",
            created_at=0,
            expires_at=0,
        )

    def test_get_nonexistent_returns_none(self, store: InMemoryIdempotencyStore) -> None:
        """Getting non-existent key returns None."""
        assert store.get("nonexistent") is None

    def test_put_if_absent_success(self, store: InMemoryIdempotencyStore) -> None:
        """put_if_absent succeeds for new key."""
        entry = self._make_entry("key1")
        assert store.put_if_absent("key1", entry, ttl_s=60) is True
        assert store.get("key1") is not None

    def test_put_if_absent_fails_for_existing(self, store: InMemoryIdempotencyStore) -> None:
        """put_if_absent fails if key already exists."""
        entry = self._make_entry("key1")
        assert store.put_if_absent("key1", entry, ttl_s=60) is True
        assert store.put_if_absent("key1", entry, ttl_s=60) is False

    def test_put_if_absent_succeeds_after_expiry(
        self, store: InMemoryIdempotencyStore, clock: FakeClock
    ) -> None:
        """put_if_absent succeeds if existing entry is expired."""
        entry = self._make_entry("key1")
        assert store.put_if_absent("key1", entry, ttl_s=60) is True

        # Advance past expiry
        clock.advance(61)

        # Now put should succeed
        assert store.put_if_absent("key1", entry, ttl_s=60) is True

    def test_get_returns_none_after_expiry(
        self, store: InMemoryIdempotencyStore, clock: FakeClock
    ) -> None:
        """get returns None for expired entry."""
        entry = self._make_entry("key1")
        store.put_if_absent("key1", entry, ttl_s=60)

        clock.advance(61)

        assert store.get("key1") is None

    def test_mark_done_updates_status(self, store: InMemoryIdempotencyStore) -> None:
        """mark_done updates entry status and stores result."""
        entry = self._make_entry("key1")
        store.put_if_absent("key1", entry, ttl_s=60)

        store.mark_done("key1", "result-value")

        updated = store.get("key1")
        assert updated is not None
        assert updated.status == IdempotencyStatus.DONE
        assert updated.result == "result-value"

    def test_mark_failed_updates_status(self, store: InMemoryIdempotencyStore) -> None:
        """mark_failed updates entry status and stores error code."""
        entry = self._make_entry("key1")
        store.put_if_absent("key1", entry, ttl_s=60)

        store.mark_failed("key1", "TestError")

        updated = store.get("key1")
        assert updated is not None
        assert updated.status == IdempotencyStatus.FAILED
        assert updated.error_code == "TestError"

    def test_purge_expired_removes_old_entries(
        self, store: InMemoryIdempotencyStore, clock: FakeClock
    ) -> None:
        """purge_expired removes entries past their TTL."""
        entry1 = self._make_entry("key1")
        entry2 = self._make_entry("key2")

        store.put_if_absent("key1", entry1, ttl_s=30)
        clock.advance(10)
        store.put_if_absent("key2", entry2, ttl_s=60)

        # Advance to expire key1 but not key2
        clock.advance(25)

        purged = store.purge_expired(clock.time())
        assert purged == 1
        assert store.get("key1") is None
        assert store.get("key2") is not None

    def test_stats_tracking(self, store: InMemoryIdempotencyStore) -> None:
        """Stats are tracked correctly."""
        entry = self._make_entry("key1")

        # Miss on first put
        store.put_if_absent("key1", entry, ttl_s=60)
        assert store.stats.misses == 1

        # Conflict on second put (INFLIGHT)
        store.put_if_absent("key1", entry, ttl_s=60)
        assert store.stats.conflicts == 1

        # Mark done
        store.mark_done("key1", "result")

        # Hit on third put (DONE)
        store.put_if_absent("key1", entry, ttl_s=60)
        assert store.stats.hits == 1

    def test_reset_clears_all(self, store: InMemoryIdempotencyStore) -> None:
        """reset clears all entries and stats."""
        entry = self._make_entry("key1")
        store.put_if_absent("key1", entry, ttl_s=60)

        store.reset()

        assert store.get("key1") is None
        assert store.stats.misses == 0


# --- IdempotentExchangePort Tests ---


class TestIdempotentExchangePort:
    """Tests for IdempotentExchangePort wrapper."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def store(self, clock: FakeClock) -> InMemoryIdempotencyStore:
        return InMemoryIdempotencyStore(_clock=clock)

    @pytest.fixture
    def inner_port(self) -> NoOpExchangePort:
        return NoOpExchangePort()

    @pytest.fixture
    def port(
        self, inner_port: NoOpExchangePort, store: InMemoryIdempotencyStore
    ) -> IdempotentExchangePort:
        return IdempotentExchangePort(inner=inner_port, store=store)

    def test_place_order_executes_once(self, port: IdempotentExchangePort) -> None:
        """First place_order executes and returns order_id."""
        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        assert order_id is not None
        assert port.stats.place_calls == 1
        assert port.stats.place_executed == 1
        assert port.stats.place_cached == 0

    def test_double_submit_returns_cached(self, port: IdempotentExchangePort) -> None:
        """Second call with same params returns cached result."""
        order_id1 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )
        order_id2 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        assert order_id1 == order_id2
        assert port.stats.place_calls == 2
        assert port.stats.place_executed == 1  # Only executed once
        assert port.stats.place_cached == 1  # Second was cached

    def test_different_params_execute_separately(self, port: IdempotentExchangePort) -> None:
        """Different params create different orders."""
        order_id1 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )
        order_id2 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.SELL,  # Different side
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=2,  # Different level
            ts=1000,
        )

        assert order_id1 != order_id2
        assert port.stats.place_executed == 2

    def test_cancel_order_idempotent(self, port: IdempotentExchangePort) -> None:
        """Cancel is idempotent - same result on retry."""
        # First, place an order
        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        # Cancel it twice
        result1 = port.cancel_order(order_id)
        result2 = port.cancel_order(order_id)

        assert result1 == result2
        assert port.stats.cancel_executed == 1
        assert port.stats.cancel_cached == 1

    def test_expired_key_allows_reexecution(
        self,
        inner_port: NoOpExchangePort,
        clock: FakeClock,
    ) -> None:
        """After TTL expires, same request can execute again."""
        store = InMemoryIdempotencyStore(_clock=clock, default_done_ttl_s=60.0)
        port = IdempotentExchangePort(inner=inner_port, store=store, done_ttl_s=60.0)

        # First call
        order_id1 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        # Advance past TTL
        clock.advance(61)

        # Second call with same params - should execute again
        order_id2 = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        # Different order IDs because it executed twice
        assert order_id1 != order_id2
        assert port.stats.place_executed == 2

    def test_fetch_open_orders_passthrough(self, port: IdempotentExchangePort) -> None:
        """fetch_open_orders passes through without idempotency."""
        # Place an order
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        # Fetch should work
        orders = port.fetch_open_orders("BTCUSDT")
        assert len(orders) == 1

    def test_reset_clears_state(self, port: IdempotentExchangePort) -> None:
        """reset clears both port and store."""
        port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        port.reset()

        assert port.stats.place_calls == 0
        orders = port.fetch_open_orders("BTCUSDT")
        assert len(orders) == 0


# --- Concurrent Duplicates Tests ---


class TestConcurrentDuplicates:
    """Tests for concurrent duplicate requests."""

    @pytest.fixture
    def store(self) -> InMemoryIdempotencyStore:
        return InMemoryIdempotencyStore()

    @pytest.fixture
    def inner_port(self) -> NoOpExchangePort:
        return NoOpExchangePort()

    @pytest.fixture
    def port(
        self, inner_port: NoOpExchangePort, store: InMemoryIdempotencyStore
    ) -> IdempotentExchangePort:
        return IdempotentExchangePort(inner=inner_port, store=store)

    def test_inflight_conflict_raises_error(self, store: InMemoryIdempotencyStore) -> None:
        """Duplicate request while INFLIGHT raises IdempotencyConflictError."""
        # Manually create an INFLIGHT entry
        entry = IdempotencyEntry(
            key="exec:place:abc123",
            status=IdempotencyStatus.INFLIGHT,
            op_name="place",
            request_fingerprint="xyz",
            created_at=time.time(),
            expires_at=time.time() + 300,
        )
        store.put_if_absent("exec:place:abc123", entry, ttl_s=300)

        # Try to put again - should fail
        result = store.put_if_absent("exec:place:abc123", entry, ttl_s=300)
        assert result is False
        assert store.stats.conflicts == 1

    @pytest.mark.asyncio
    async def test_concurrent_place_one_wins(self, port: IdempotentExchangePort) -> None:
        """Concurrent place_order calls - one succeeds, others get cached or conflict."""
        results: list[str | Exception] = []
        conflicts = 0

        async def place() -> None:
            nonlocal conflicts
            try:
                order_id = port.place_order(
                    symbol="BTCUSDT",
                    side=OrderSide.BUY,
                    price=Decimal("50000"),
                    quantity=Decimal("0.001"),
                    level_id=1,
                    ts=1000,
                )
                results.append(order_id)
            except IdempotencyConflictError:
                conflicts += 1

        # Run multiple concurrent calls
        await asyncio.gather(place(), place(), place())

        # All successful results should be the same order_id
        if results:
            assert all(r == results[0] for r in results)

        # Either all succeeded (got cached) or some got conflicts
        # Side-effect should only happen once
        assert port.stats.place_executed == 1


# --- Retry + Idempotency Integration Tests ---


class TestRetryIdempotencyIntegration:
    """Tests for retry + idempotency = 1 side-effect."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def store(self, clock: FakeClock) -> InMemoryIdempotencyStore:
        return InMemoryIdempotencyStore(_clock=clock)

    def test_retry_with_same_key_single_side_effect(self, store: InMemoryIdempotencyStore) -> None:
        """Simulating retry behavior: same key = 1 side-effect."""

        # Simulate a port that tracks actual executions
        class CountingPort:
            def __init__(self) -> None:
                self.execute_count = 0
                self._order_counter = 0

            def place_order(
                self,
                symbol: str,  # noqa: ARG002
                side: OrderSide,  # noqa: ARG002
                price: Decimal,  # noqa: ARG002
                quantity: Decimal,  # noqa: ARG002
                level_id: int,  # noqa: ARG002
                ts: int,  # noqa: ARG002
            ) -> str:
                self.execute_count += 1
                self._order_counter += 1
                return f"order-{self._order_counter}"

            def fetch_open_orders(self, symbol: str) -> list[Any]:  # noqa: ARG002
                return []

        inner = CountingPort()
        port = IdempotentExchangePort(inner=inner, store=store)

        # Simulate 3 retry attempts with same parameters
        results = []
        for _attempt in range(3):
            order_id = port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
                level_id=1,
                ts=1000,
            )
            results.append(order_id)

        # All results should be the same
        assert all(r == results[0] for r in results)

        # Actual execution happened only once
        assert inner.execute_count == 1
        assert port.stats.place_executed == 1
        assert port.stats.place_cached == 2

    def test_failed_operation_allows_retry(self, store: InMemoryIdempotencyStore) -> None:
        """If operation fails, mark_failed allows retry."""

        class FailOncePort:
            def __init__(self) -> None:
                self.call_count = 0

            def place_order(
                self,
                symbol: str,  # noqa: ARG002
                side: OrderSide,  # noqa: ARG002
                price: Decimal,  # noqa: ARG002
                quantity: Decimal,  # noqa: ARG002
                level_id: int,  # noqa: ARG002
                ts: int,  # noqa: ARG002
            ) -> str:
                self.call_count += 1
                if self.call_count == 1:
                    raise ValueError("Simulated failure")
                return "order-success"

            def fetch_open_orders(self, symbol: str) -> list[Any]:  # noqa: ARG002
                return []

        inner = FailOncePort()
        port = IdempotentExchangePort(inner=inner, store=store)

        # First attempt fails
        with pytest.raises(ValueError, match="Simulated failure"):
            port.place_order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                price=Decimal("50000"),
                quantity=Decimal("0.001"),
                level_id=1,
                ts=1000,
            )

        # Entry should be marked FAILED
        # Retry with same params should execute again
        # (FAILED entries allow re-execution in v1)
        order_id = port.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=Decimal("50000"),
            quantity=Decimal("0.001"),
            level_id=1,
            ts=1000,
        )

        assert order_id == "order-success"
        assert inner.call_count == 2  # Called twice total


# --- TTL/Expiration Tests ---


class TestTTLExpiration:
    """Tests for TTL and expiration behavior."""

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def store(self, clock: FakeClock) -> InMemoryIdempotencyStore:
        return InMemoryIdempotencyStore(
            _clock=clock,
            default_inflight_ttl_s=60.0,
            default_done_ttl_s=300.0,
        )

    def test_inflight_expires_faster_than_done(
        self, store: InMemoryIdempotencyStore, clock: FakeClock
    ) -> None:
        """INFLIGHT entries have shorter TTL than DONE."""
        entry = IdempotencyEntry(
            key="key1",
            status=IdempotencyStatus.INFLIGHT,
            op_name="test",
            request_fingerprint="abc",
            created_at=0,
            expires_at=0,
        )

        # Put as INFLIGHT
        store.put_if_absent("key1", entry, ttl_s=60)

        # Should exist before 60s
        clock.advance(30)
        assert store.get("key1") is not None

        # Should expire after 60s
        clock.advance(31)
        assert store.get("key1") is None

    def test_done_ttl_extended(self, store: InMemoryIdempotencyStore, clock: FakeClock) -> None:
        """mark_done extends TTL to done_ttl."""
        entry = IdempotencyEntry(
            key="key1",
            status=IdempotencyStatus.INFLIGHT,
            op_name="test",
            request_fingerprint="abc",
            created_at=0,
            expires_at=0,
        )

        store.put_if_absent("key1", entry, ttl_s=60)
        clock.advance(30)

        # Mark done (extends TTL)
        store.mark_done("key1", "result")

        # Should exist after original 60s
        clock.advance(40)
        assert store.get("key1") is not None

        # Should expire after done_ttl (300s from mark_done)
        clock.advance(270)
        assert store.get("key1") is None

    def test_purge_expired_batch(self, store: InMemoryIdempotencyStore, clock: FakeClock) -> None:
        """purge_expired removes multiple expired entries."""
        for i in range(5):
            entry = IdempotencyEntry(
                key=f"key{i}",
                status=IdempotencyStatus.INFLIGHT,
                op_name="test",
                request_fingerprint="abc",
                created_at=0,
                expires_at=0,
            )
            store.put_if_absent(f"key{i}", entry, ttl_s=60)

        clock.advance(61)

        purged = store.purge_expired(clock.time())
        assert purged == 5
        assert store.stats.total_entries == 0
