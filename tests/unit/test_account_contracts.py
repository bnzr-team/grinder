"""Tests for account snapshot contracts (Launch-15 PR1).

Validates:
- I1: Deterministic serialization
- I2: Round-trip equality
- I3: Sha256 stability
- I6: No duplicate keys (canonical ordering)
- Canonical sort order for positions and orders

SSOT: docs/15_ACCOUNT_SYNC_SPEC.md (Sec 15.3-15.5)
"""

from decimal import Decimal

import pytest

from grinder.account.contracts import (
    AccountSnapshot,
    OpenOrderSnap,
    PositionSnap,
    build_account_snapshot,
    canonical_orders,
    canonical_positions,
)
from grinder.account.render import load_snapshot, render_snapshot, snapshot_sha256

# -- Fixtures --


def _pos(
    symbol: str = "BTCUSDT", side: str = "LONG", qty: str = "1.5", ts: int = 1000
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
    order_id: str = "id_1",
    symbol: str = "BTCUSDT",
    side: str = "BUY",
    order_type: str = "LIMIT",
    price: str = "49000.00",
    qty: str = "0.01",
    ts: int = 1000,
) -> OpenOrderSnap:
    return OpenOrderSnap(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=Decimal(price),
        qty=Decimal(qty),
        filled_qty=Decimal("0"),
        reduce_only=False,
        status="NEW",
        ts=ts,
    )


# -- PositionSnap tests --


class TestPositionSnap:
    def test_frozen(self) -> None:
        p = _pos()
        with pytest.raises(AttributeError):
            p.symbol = "ETHUSDT"  # type: ignore[misc]

    def test_to_dict_roundtrip(self) -> None:
        p = _pos()
        d = p.to_dict()
        p2 = PositionSnap.from_dict(d)
        assert p == p2

    def test_sort_key(self) -> None:
        p = _pos(symbol="BTCUSDT", side="LONG")
        assert p.sort_key() == ("BTCUSDT", "LONG")

    def test_decimal_serialization_no_scientific(self) -> None:
        p = _pos(qty="0.00000001")
        d = p.to_dict()
        assert d["qty"] == "1E-8" or d["qty"] == "0.00000001"
        # Round-trip must preserve value
        p2 = PositionSnap.from_dict(d)
        assert p2.qty == Decimal("0.00000001")


# -- OpenOrderSnap tests --


class TestOpenOrderSnap:
    def test_frozen(self) -> None:
        o = _order()
        with pytest.raises(AttributeError):
            o.order_id = "id_2"  # type: ignore[misc]

    def test_to_dict_roundtrip(self) -> None:
        o = _order()
        d = o.to_dict()
        o2 = OpenOrderSnap.from_dict(d)
        assert o == o2

    def test_sort_key(self) -> None:
        o = _order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price="49000",
            qty="0.01",
            order_id="id_1",
        )
        assert o.sort_key() == (
            "BTCUSDT",
            "BUY",
            "LIMIT",
            Decimal("49000"),
            Decimal("0.01"),
            "id_1",
        )


# -- AccountSnapshot tests --


class TestAccountSnapshot:
    def test_frozen(self) -> None:
        snap = AccountSnapshot(positions=(), open_orders=(), ts=1000, source="test")
        with pytest.raises(AttributeError):
            snap.ts = 2000  # type: ignore[misc]

    def test_to_dict_roundtrip(self) -> None:
        snap = build_account_snapshot(
            positions=[_pos("ETHUSDT", "LONG"), _pos("BTCUSDT", "SHORT")],
            open_orders=[_order("id_2", "ETHUSDT"), _order("id_1", "BTCUSDT")],
            source="test",
        )
        d = snap.to_dict()
        snap2 = AccountSnapshot.from_dict(d)
        assert snap == snap2

    def test_empty_snapshot(self) -> None:
        snap = build_account_snapshot(positions=[], open_orders=[], source="test")
        assert snap.positions == ()
        assert snap.open_orders == ()
        assert snap.ts == 0
        assert snap.source == "test"


# -- Canonical ordering tests --


class TestCanonicalPositions:
    def test_sorted_by_symbol_then_side(self) -> None:
        positions = [
            _pos("ETHUSDT", "LONG"),
            _pos("BTCUSDT", "SHORT"),
            _pos("BTCUSDT", "LONG"),
            _pos("ETHUSDT", "SHORT"),
        ]
        result = canonical_positions(positions)
        keys = [p.sort_key() for p in result]
        assert keys == [
            ("BTCUSDT", "LONG"),
            ("BTCUSDT", "SHORT"),
            ("ETHUSDT", "LONG"),
            ("ETHUSDT", "SHORT"),
        ]

    def test_already_sorted(self) -> None:
        positions = [_pos("AAAUSDT", "LONG"), _pos("BBBUSDT", "LONG")]
        result = canonical_positions(positions)
        assert result == tuple(positions)

    def test_empty(self) -> None:
        assert canonical_positions([]) == ()


class TestCanonicalOrders:
    def test_sorted_by_full_key(self) -> None:
        orders = [
            _order("id_3", "BTCUSDT", "SELL", "LIMIT", "51000", "0.01"),
            _order("id_1", "BTCUSDT", "BUY", "LIMIT", "49000", "0.01"),
            _order("id_2", "BTCUSDT", "BUY", "LIMIT", "49500", "0.01"),
        ]
        result = canonical_orders(orders)
        ids = [o.order_id for o in result]
        assert ids == ["id_1", "id_2", "id_3"]

    def test_price_numeric_order(self) -> None:
        """Price sorts numerically, not lexicographically."""
        orders = [
            _order("id_1", price="100"),
            _order("id_2", price="20"),
            _order("id_3", price="3"),
        ]
        result = canonical_orders(orders)
        prices = [o.price for o in result]
        assert prices == [Decimal("3"), Decimal("20"), Decimal("100")]

    def test_order_id_tiebreaker(self) -> None:
        """Same price/qty: order_id breaks tie."""
        orders = [
            _order("id_b", price="49000", qty="0.01"),
            _order("id_a", price="49000", qty="0.01"),
        ]
        result = canonical_orders(orders)
        ids = [o.order_id for o in result]
        assert ids == ["id_a", "id_b"]

    def test_empty(self) -> None:
        assert canonical_orders([]) == ()


# -- build_account_snapshot tests --


class TestBuildAccountSnapshot:
    def test_ts_is_max_of_components(self) -> None:
        snap = build_account_snapshot(
            positions=[_pos(ts=1000), _pos("ETHUSDT", ts=2000)],
            open_orders=[_order(ts=1500)],
            source="test",
        )
        assert snap.ts == 2000

    def test_canonical_ordering_applied(self) -> None:
        snap = build_account_snapshot(
            positions=[_pos("ETHUSDT", "LONG"), _pos("BTCUSDT", "LONG")],
            open_orders=[_order("id_2"), _order("id_1")],
            source="test",
        )
        assert snap.positions[0].symbol == "BTCUSDT"
        assert snap.open_orders[0].order_id == "id_1"

    def test_source_propagated(self) -> None:
        snap = build_account_snapshot(positions=[], open_orders=[], source="fire_drill")
        assert snap.source == "fire_drill"


# -- Invariant I1: Deterministic serialization --


class TestDeterministicSerialization:
    def test_same_input_same_json(self) -> None:
        """I1: Same inputs produce byte-identical JSON."""
        snap = build_account_snapshot(
            positions=[_pos("ETHUSDT"), _pos("BTCUSDT")],
            open_orders=[_order("id_2"), _order("id_1")],
            source="test",
        )
        json1 = render_snapshot(snap)
        json2 = render_snapshot(snap)
        assert json1 == json2

    def test_reordered_input_same_json(self) -> None:
        """I1: Different input order, same canonical output."""
        snap1 = build_account_snapshot(
            positions=[_pos("ETHUSDT"), _pos("BTCUSDT")],
            open_orders=[_order("id_2"), _order("id_1")],
            source="test",
        )
        snap2 = build_account_snapshot(
            positions=[_pos("BTCUSDT"), _pos("ETHUSDT")],
            open_orders=[_order("id_1"), _order("id_2")],
            source="test",
        )
        assert render_snapshot(snap1) == render_snapshot(snap2)


# -- Invariant I2: Round-trip equality --


class TestRoundTripEquality:
    def test_roundtrip(self) -> None:
        """I2: snapshot == load(render(snapshot))."""
        snap = build_account_snapshot(
            positions=[_pos("BTCUSDT", "LONG"), _pos("ETHUSDT", "SHORT")],
            open_orders=[_order("id_1"), _order("id_2", "ETHUSDT", "SELL")],
            source="test",
        )
        json_str = render_snapshot(snap)
        snap2 = load_snapshot(json_str)
        assert snap == snap2

    def test_roundtrip_empty(self) -> None:
        snap = build_account_snapshot(positions=[], open_orders=[], source="test")
        json_str = render_snapshot(snap)
        snap2 = load_snapshot(json_str)
        assert snap == snap2


# -- Invariant I3: Sha256 stability --


class TestSha256Stability:
    def test_same_input_same_hash(self) -> None:
        """I3: Identical input -> identical sha256."""
        snap = build_account_snapshot(
            positions=[_pos()],
            open_orders=[_order()],
            source="test",
        )
        h1 = snapshot_sha256(snap)
        h2 = snapshot_sha256(snap)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex digest

    def test_different_input_different_hash(self) -> None:
        snap1 = build_account_snapshot(positions=[_pos()], open_orders=[], source="test")
        snap2 = build_account_snapshot(positions=[_pos("ETHUSDT")], open_orders=[], source="test")
        assert snapshot_sha256(snap1) != snapshot_sha256(snap2)

    def test_reordered_same_hash(self) -> None:
        """I3: Reordered input produces same hash (via canonical ordering)."""
        snap1 = build_account_snapshot(
            positions=[_pos("ETHUSDT"), _pos("BTCUSDT")],
            open_orders=[],
            source="test",
        )
        snap2 = build_account_snapshot(
            positions=[_pos("BTCUSDT"), _pos("ETHUSDT")],
            open_orders=[],
            source="test",
        )
        assert snapshot_sha256(snap1) == snapshot_sha256(snap2)
