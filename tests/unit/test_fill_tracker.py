"""Tests for grinder.execution.fill_tracker (Launch-06 PR1).

Covers:
- FillEvent creation
- FillTracker.record() aggregation (counts, qty, notional, fees)
- Maker/taker and buy/sell splits
- Reject NaN/Inf/negative values
- FillSnapshot immutability
- Per-label tracking
"""

from __future__ import annotations

import pytest

from grinder.execution.fill_tracker import (
    FillEvent,
    FillSnapshot,
    FillTracker,
    FillValidationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    *,
    ts_ms: int = 1000,
    source: str = "sim",
    side: str = "buy",
    liquidity: str = "taker",
    qty: float = 1.0,
    price: float = 100.0,
    fee: float = 0.1,
) -> FillEvent:
    return FillEvent(
        ts_ms=ts_ms,
        source=source,
        side=side,
        liquidity=liquidity,
        qty=qty,
        price=price,
        fee=fee,
    )


# ---------------------------------------------------------------------------
# FillEvent tests
# ---------------------------------------------------------------------------


class TestFillEvent:
    """FillEvent is frozen and holds correct values."""

    def test_creation(self) -> None:
        e = _make_fill()
        assert e.ts_ms == 1000
        assert e.source == "sim"
        assert e.side == "buy"
        assert e.liquidity == "taker"
        assert e.qty == 1.0
        assert e.price == 100.0
        assert e.fee == 0.1
        assert e.fee_asset == ""

    def test_frozen(self) -> None:
        e = _make_fill()
        with pytest.raises(AttributeError):
            e.qty = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FillTracker aggregation tests
# ---------------------------------------------------------------------------


class TestFillTrackerAggregation:
    """FillTracker correctly aggregates fill events."""

    def test_single_fill(self) -> None:
        t = FillTracker()
        t.record(_make_fill(qty=2.0, price=50.0, fee=0.5))
        snap = t.snapshot()
        assert snap.total_fills == 1
        assert snap.total_qty == 2.0
        assert snap.total_notional == 100.0  # 2.0 * 50.0
        assert snap.total_fees == 0.5

    def test_multiple_fills(self) -> None:
        t = FillTracker()
        t.record(_make_fill(qty=1.0, price=100.0, fee=0.1))
        t.record(_make_fill(qty=2.0, price=200.0, fee=0.2))
        t.record(_make_fill(qty=3.0, price=300.0, fee=0.3))
        snap = t.snapshot()
        assert snap.total_fills == 3
        assert snap.total_qty == 6.0
        assert snap.total_notional == pytest.approx(100.0 + 400.0 + 900.0)
        assert snap.total_fees == pytest.approx(0.6)

    def test_empty_tracker(self) -> None:
        snap = FillTracker().snapshot()
        assert snap.total_fills == 0
        assert snap.total_qty == 0.0
        assert snap.total_notional == 0.0
        assert snap.total_fees == 0.0


# ---------------------------------------------------------------------------
# Buy/sell split tests
# ---------------------------------------------------------------------------


class TestBuySellSplit:
    """FillTracker correctly splits buy vs sell."""

    def test_buy_only(self) -> None:
        t = FillTracker()
        t.record(_make_fill(side="buy", qty=1.0, price=100.0))
        snap = t.snapshot()
        assert snap.buy_fills == 1
        assert snap.sell_fills == 0
        assert snap.buy_notional == 100.0
        assert snap.sell_notional == 0.0

    def test_sell_only(self) -> None:
        t = FillTracker()
        t.record(_make_fill(side="sell", qty=2.0, price=50.0))
        snap = t.snapshot()
        assert snap.buy_fills == 0
        assert snap.sell_fills == 1
        assert snap.sell_notional == 100.0

    def test_mixed(self) -> None:
        t = FillTracker()
        t.record(_make_fill(side="buy", qty=1.0, price=100.0))
        t.record(_make_fill(side="sell", qty=2.0, price=50.0))
        t.record(_make_fill(side="buy", qty=3.0, price=10.0))
        snap = t.snapshot()
        assert snap.buy_fills == 2
        assert snap.sell_fills == 1
        assert snap.buy_notional == pytest.approx(130.0)
        assert snap.sell_notional == pytest.approx(100.0)

    def test_none_side_not_counted(self) -> None:
        t = FillTracker()
        t.record(_make_fill(side="none", qty=1.0, price=100.0))
        snap = t.snapshot()
        assert snap.total_fills == 1
        assert snap.buy_fills == 0
        assert snap.sell_fills == 0


# ---------------------------------------------------------------------------
# Maker/taker split tests
# ---------------------------------------------------------------------------


class TestMakerTakerSplit:
    """FillTracker correctly splits maker vs taker."""

    def test_maker_only(self) -> None:
        t = FillTracker()
        t.record(_make_fill(liquidity="maker", qty=1.0, price=100.0))
        snap = t.snapshot()
        assert snap.maker_fills == 1
        assert snap.taker_fills == 0
        assert snap.maker_notional == 100.0

    def test_taker_only(self) -> None:
        t = FillTracker()
        t.record(_make_fill(liquidity="taker", qty=2.0, price=50.0))
        snap = t.snapshot()
        assert snap.taker_fills == 1
        assert snap.taker_notional == 100.0

    def test_mixed(self) -> None:
        t = FillTracker()
        t.record(_make_fill(liquidity="maker", qty=1.0, price=100.0))
        t.record(_make_fill(liquidity="taker", qty=2.0, price=50.0))
        snap = t.snapshot()
        assert snap.maker_fills == 1
        assert snap.taker_fills == 1
        assert snap.maker_notional == 100.0
        assert snap.taker_notional == 100.0

    def test_none_liquidity_not_counted(self) -> None:
        t = FillTracker()
        t.record(_make_fill(liquidity="none", qty=1.0, price=100.0))
        snap = t.snapshot()
        assert snap.total_fills == 1
        assert snap.maker_fills == 0
        assert snap.taker_fills == 0


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestFillValidation:
    """FillTracker.record() rejects invalid data."""

    def test_nan_qty(self) -> None:
        t = FillTracker()
        with pytest.raises(FillValidationError, match="qty"):
            t.record(_make_fill(qty=float("nan")))

    def test_inf_price(self) -> None:
        t = FillTracker()
        with pytest.raises(FillValidationError, match="price"):
            t.record(_make_fill(price=float("inf")))

    def test_neg_inf_fee(self) -> None:
        t = FillTracker()
        with pytest.raises(FillValidationError, match="fee"):
            t.record(_make_fill(fee=float("-inf")))

    def test_negative_qty(self) -> None:
        t = FillTracker()
        with pytest.raises(FillValidationError, match="qty"):
            t.record(_make_fill(qty=-1.0))

    def test_negative_price(self) -> None:
        t = FillTracker()
        with pytest.raises(FillValidationError, match="price"):
            t.record(_make_fill(price=-0.01))

    def test_negative_fee(self) -> None:
        t = FillTracker()
        with pytest.raises(FillValidationError, match="fee"):
            t.record(_make_fill(fee=-0.001))

    def test_zero_values_ok(self) -> None:
        """qty=0, price=0, fee=0 are valid (e.g. zero-fee fills)."""
        t = FillTracker()
        t.record(_make_fill(qty=0.0, price=0.0, fee=0.0))
        snap = t.snapshot()
        assert snap.total_fills == 1
        assert snap.total_notional == 0.0


# ---------------------------------------------------------------------------
# Per-label tracking tests
# ---------------------------------------------------------------------------


class TestPerLabelTracking:
    """FillTracker tracks per-(source, side, liquidity) counters."""

    def test_label_counts(self) -> None:
        t = FillTracker()
        t.record(_make_fill(source="sim", side="buy", liquidity="taker", qty=1.0, price=100.0))
        t.record(_make_fill(source="sim", side="buy", liquidity="taker", qty=2.0, price=50.0))
        t.record(
            _make_fill(source="reconcile", side="sell", liquidity="maker", qty=3.0, price=10.0)
        )

        assert t._fills_by_label[("sim", "buy", "taker")] == 2
        assert t._fills_by_label[("reconcile", "sell", "maker")] == 1
        assert t._notional_by_label[("sim", "buy", "taker")] == pytest.approx(200.0)
        assert t._notional_by_label[("reconcile", "sell", "maker")] == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Snapshot immutability tests
# ---------------------------------------------------------------------------


class TestFillSnapshot:
    """FillSnapshot is frozen."""

    def test_frozen(self) -> None:
        snap = FillSnapshot()
        with pytest.raises(AttributeError):
            snap.total_fills = 5  # type: ignore[misc]
