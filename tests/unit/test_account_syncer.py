"""Tests for AccountSyncer mismatch detection (Launch-15 PR2).

Validates:
- Mismatch rules: duplicate_key, ts_regression, negative_qty, orphan_order
- Metrics recording on sync
- Monotonic ts guard (I5)
- Clean sync (no mismatches)
- Fetch error handling

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.5-15.7)
"""

from decimal import Decimal

import pytest

from grinder.account.contracts import (
    AccountSnapshot,
    OpenOrderSnap,
    PositionSnap,
)
from grinder.account.metrics import get_account_sync_metrics, reset_account_sync_metrics
from grinder.account.syncer import AccountSyncer, Mismatch, SyncResult
from grinder.execution.port import NoOpExchangePort

# -- Helpers --


def _pos(
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    qty: str = "1.5",
    ts: int = 1000,
) -> PositionSnap:
    return PositionSnap(
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        entry_price=Decimal("50000.00"),
        mark_price=Decimal("50100.00"),
        unrealized_pnl=Decimal("150.00"),
        leverage=10,
        ts=ts,
    )


def _order(
    order_id: str = "ord_1",
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    price: str = "49000.00",
    qty: str = "0.01",
    filled_qty: str = "0",
    ts: int = 1000,
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=Decimal(price),
        qty=Decimal(qty),
        filled_qty=Decimal(filled_qty),
        reduce_only=False,
        status="NEW",
        ts=ts,
    )


def _snapshot(
    positions: tuple[PositionSnap, ...] = (),
    open_orders: tuple[OpenOrderSnap, ...] = (),
    ts: int = 1000,
    source: str = "test",
) -> AccountSnapshot:
    return AccountSnapshot(
        positions=positions,
        open_orders=open_orders,
        ts=ts,
        source=source,
    )


class FakePort:
    """Fake ExchangePort that returns a configured snapshot."""

    def __init__(
        self, snapshot: AccountSnapshot | None = None, error: Exception | None = None
    ) -> None:
        self._snapshot = snapshot
        self._error = error

    def fetch_account_snapshot(self) -> AccountSnapshot:
        if self._error is not None:
            raise self._error
        assert self._snapshot is not None
        return self._snapshot

    def place_order(self, **_kwargs: object) -> str:
        return "fake_id"

    def cancel_order(self, _order_id: str) -> bool:
        return True

    def replace_order(self, **_kwargs: object) -> str:
        return "fake_id"

    def fetch_open_orders(self, _symbol: str) -> list[object]:
        return []

    def fetch_positions(self) -> list[PositionSnap]:
        return []


# -- Fixtures --


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_account_sync_metrics()


# -- Tests: Clean sync --


class TestCleanSync:
    def test_clean_sync_no_mismatches(self) -> None:
        snap = _snapshot(
            positions=(_pos(),),
            open_orders=(_order(),),
            ts=2000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()

        assert result.ok
        assert result.snapshot == snap
        assert result.mismatches == []
        assert result.error is None

    def test_clean_sync_updates_last_ts(self) -> None:
        snap = _snapshot(ts=5000)
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        syncer.sync()
        assert syncer.last_ts == 5000

    def test_clean_sync_records_metrics(self) -> None:
        snap = _snapshot(
            positions=(_pos(), _pos(symbol="ETHUSDT")),
            open_orders=(_order(),),
            ts=3000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        syncer.sync()

        metrics = get_account_sync_metrics()
        assert metrics.last_sync_ts == 3000
        assert metrics.positions_count == 2
        assert metrics.open_orders_count == 1

    def test_empty_snapshot_ok(self) -> None:
        snap = _snapshot(ts=1000)
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()
        assert result.ok
        assert result.snapshot is not None
        assert len(result.snapshot.positions) == 0
        assert len(result.snapshot.open_orders) == 0


# -- Tests: Mismatch detection --


class TestDuplicateKey:
    def test_duplicate_position_key(self) -> None:
        snap = _snapshot(
            positions=(_pos(symbol="BTCUSDT", side="LONG"), _pos(symbol="BTCUSDT", side="LONG")),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()

        assert not result.ok
        rules = [m.rule for m in result.mismatches]
        assert "duplicate_key" in rules

    def test_duplicate_order_id(self) -> None:
        snap = _snapshot(
            open_orders=(_order(order_id="dup"), _order(order_id="dup")),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()

        rules = [m.rule for m in result.mismatches]
        assert "duplicate_key" in rules

    def test_no_duplicate_different_keys(self) -> None:
        snap = _snapshot(
            positions=(_pos(side="LONG"), _pos(side="SHORT")),
            open_orders=(_order(order_id="a"), _order(order_id="b")),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()
        assert result.ok


class TestTsRegression:
    def test_ts_regression_detected(self) -> None:
        port1 = FakePort(snapshot=_snapshot(ts=5000))
        syncer = AccountSyncer(port1)  # type: ignore[arg-type]
        syncer.sync()  # establishes last_ts=5000

        # Now feed older snapshot
        port2 = FakePort(snapshot=_snapshot(ts=3000))
        syncer._port = port2  # type: ignore[assignment]

        result = syncer.sync()
        rules = [m.rule for m in result.mismatches]
        assert "ts_regression" in rules

    def test_ts_regression_does_not_update_last_ts(self) -> None:
        port1 = FakePort(snapshot=_snapshot(ts=5000))
        syncer = AccountSyncer(port1)  # type: ignore[arg-type]
        syncer.sync()

        port2 = FakePort(snapshot=_snapshot(ts=3000))
        syncer._port = port2  # type: ignore[assignment]
        syncer.sync()

        # last_ts should still be 5000 (regression rejected)
        assert syncer.last_ts == 5000

    def test_equal_ts_no_regression(self) -> None:
        port = FakePort(snapshot=_snapshot(ts=5000))
        syncer = AccountSyncer(port)  # type: ignore[arg-type]
        syncer.sync()

        result = syncer.sync()  # same ts=5000
        rules = [m.rule for m in result.mismatches]
        assert "ts_regression" not in rules


class TestNegativeQty:
    def test_negative_position_qty(self) -> None:
        snap = _snapshot(
            positions=(_pos(qty="-1"),),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()
        rules = [m.rule for m in result.mismatches]
        assert "negative_qty" in rules

    def test_negative_order_qty(self) -> None:
        snap = _snapshot(
            open_orders=(_order(qty="-0.5"),),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()
        rules = [m.rule for m in result.mismatches]
        assert "negative_qty" in rules

    def test_zero_qty_no_mismatch(self) -> None:
        snap = _snapshot(
            positions=(_pos(qty="0"),),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()
        rules = [m.rule for m in result.mismatches]
        assert "negative_qty" not in rules


class TestOrphanOrder:
    def test_orphan_order_detected(self) -> None:
        snap = _snapshot(
            open_orders=(_order(order_id="exchange_only"),),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync(known_order_ids=frozenset({"internal_1", "internal_2"}))
        rules = [m.rule for m in result.mismatches]
        assert "orphan_order" in rules

    def test_no_orphan_when_known(self) -> None:
        snap = _snapshot(
            open_orders=(_order(order_id="known_1"),),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync(known_order_ids=frozenset({"known_1"}))
        rules = [m.rule for m in result.mismatches]
        assert "orphan_order" not in rules

    def test_orphan_check_skipped_without_known_ids(self) -> None:
        snap = _snapshot(
            open_orders=(_order(order_id="any"),),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync(known_order_ids=None)
        rules = [m.rule for m in result.mismatches]
        assert "orphan_order" not in rules


# -- Tests: Fetch errors --


class TestFetchError:
    def test_fetch_error_recorded(self) -> None:
        port = FakePort(error=ConnectionError("timeout"))
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        result = syncer.sync()

        assert result.error is not None
        assert "ConnectionError" in result.error
        assert result.snapshot is None
        assert not result.ok

    def test_fetch_error_increments_metric(self) -> None:
        port = FakePort(error=ValueError("bad data"))
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        syncer.sync()

        metrics = get_account_sync_metrics()
        assert metrics.sync_errors.get("ValueError") == 1


# -- Tests: Pending notional --


class TestPendingNotional:
    def test_pending_notional_computed(self) -> None:
        snap = _snapshot(
            open_orders=(
                _order(price="50000", qty="0.1", filled_qty="0"),
                _order(order_id="ord_2", price="49000", qty="0.05", filled_qty="0.01"),
            ),
            ts=1000,
        )
        port = FakePort(snapshot=snap)
        syncer = AccountSyncer(port)  # type: ignore[arg-type]

        syncer.sync()

        metrics = get_account_sync_metrics()
        # 50000 * 0.1 + 49000 * 0.04 = 5000 + 1960 = 6960
        assert metrics.pending_notional == pytest.approx(6960.0)


# -- Tests: Mismatch serialization --


class TestMismatchSerialization:
    def test_mismatch_to_dict(self) -> None:
        m = Mismatch(rule="duplicate_key", detail="dup pos BTCUSDT/LONG")
        d = m.to_dict()
        assert d == {"rule": "duplicate_key", "detail": "dup pos BTCUSDT/LONG"}


# -- Tests: SyncResult --


class TestSyncResult:
    def test_ok_true_clean(self) -> None:
        r = SyncResult(snapshot=_snapshot())
        assert r.ok

    def test_ok_false_with_mismatches(self) -> None:
        r = SyncResult(
            snapshot=_snapshot(),
            mismatches=[Mismatch(rule="x", detail="y")],
        )
        assert not r.ok

    def test_ok_false_with_error(self) -> None:
        r = SyncResult(error="boom")
        assert not r.ok

    def test_ok_false_no_snapshot(self) -> None:
        r = SyncResult()
        assert not r.ok


# -- Tests: Reset --


class TestReset:
    def test_reset_clears_last_ts(self) -> None:
        port = FakePort(snapshot=_snapshot(ts=5000))
        syncer = AccountSyncer(port)  # type: ignore[arg-type]
        syncer.sync()
        assert syncer.last_ts == 5000

        syncer.reset()
        assert syncer.last_ts == 0


# -- Tests: NoOpExchangePort integration --


class TestNoOpPortIntegration:
    def test_noop_port_clean_sync(self) -> None:
        """NoOpExchangePort.fetch_account_snapshot() returns empty stub."""
        port = NoOpExchangePort()
        syncer = AccountSyncer(port)

        result = syncer.sync()
        assert result.ok
        assert result.snapshot is not None
        assert result.snapshot.source == "stub"
        assert len(result.snapshot.positions) == 0
        assert len(result.snapshot.open_orders) == 0


# -- Tests: compute_position_notional --


class TestComputePositionNotional:
    """Tests for AccountSyncer.compute_position_notional() static method."""

    def test_two_positions_notional(self) -> None:
        """BTCUSDT 0.002 @ 65000 + ETHUSDT 0.01 @ 3500 = 130 + 35 = 165.0."""
        snap = _snapshot(
            positions=(
                PositionSnap(
                    symbol="BTCUSDT",
                    side="LONG",
                    qty=Decimal("0.002"),
                    entry_price=Decimal("64000"),
                    mark_price=Decimal("65000"),
                    unrealized_pnl=Decimal("2.0"),
                    leverage=10,
                    ts=1000,
                ),
                PositionSnap(
                    symbol="ETHUSDT",
                    side="LONG",
                    qty=Decimal("0.01"),
                    entry_price=Decimal("3400"),
                    mark_price=Decimal("3500"),
                    unrealized_pnl=Decimal("1.0"),
                    leverage=5,
                    ts=1000,
                ),
            ),
            ts=1000,
        )
        result = AccountSyncer.compute_position_notional(snap)
        assert result == pytest.approx(165.0)

    def test_empty_positions_returns_zero(self) -> None:
        """No positions → 0.0 notional."""
        snap = _snapshot(ts=1000)
        result = AccountSyncer.compute_position_notional(snap)
        assert result == pytest.approx(0.0)

    def test_noop_port_empty_snapshot_notional(self) -> None:
        """NoOpExchangePort → empty positions → 0.0 notional (deterministic)."""
        port = NoOpExchangePort()
        syncer = AccountSyncer(port)
        sync_result = syncer.sync()
        assert sync_result.snapshot is not None
        notional = AccountSyncer.compute_position_notional(sync_result.snapshot)
        assert notional == pytest.approx(0.0)
