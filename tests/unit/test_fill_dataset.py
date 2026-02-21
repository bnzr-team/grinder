"""Tests for grinder.ml.fill_dataset (Track C, PR-C1).

Covers:
- FillOutcomeRow: field presence, frozen, to_dict roundtrip.
- RoundtripTracker: basic roundtrip, entry/exit detection,
  partial adds, multi-symbol, short positions, fee handling.
- _compute_row_id: determinism.
- build_fill_dataset_v1: manifest schema, artifact integrity,
  determinism rebuild, empty dataset, force overwrite.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from scripts.build_fill_dataset_v1 import _load_fills

from grinder.ml.fill_dataset import (
    FILL_OUTCOME_COLUMNS,
    FillOutcomeRow,
    RoundtripTracker,
    _compute_row_id,
    build_fill_dataset_v1,
)
from grinder.paper.fills import Fill

if TYPE_CHECKING:
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------

_D = Decimal


def _fill(
    ts: int,
    symbol: str,
    side: str,
    price: str,
    quantity: str,
    order_id: str = "",
) -> Fill:
    """Convenience constructor for test fills."""
    return Fill(
        ts=ts,
        symbol=symbol,
        side=side,
        price=_D(price),
        quantity=_D(quantity),
        order_id=order_id or f"ord_{ts}_{side}",
    )


# --- FillOutcomeRow --------------------------------------------------------


class TestFillOutcomeRowFields:
    """REQ-001: FillOutcomeRow is a frozen dataclass with expected fields."""

    def test_all_columns_present(self) -> None:
        """Every column in FILL_OUTCOME_COLUMNS corresponds to a field."""
        row = FillOutcomeRow(
            row_id="abc123",
            symbol="BTCUSDT",
            direction="long",
            entry_ts=1000,
            entry_price=_D("50000"),
            entry_qty=_D("0.1"),
            entry_fee=_D("5"),
            entry_fill_count=1,
            exit_ts=2000,
            exit_price=_D("51000"),
            exit_qty=_D("0.1"),
            exit_fee=_D("5.1"),
            exit_fill_count=1,
            realized_pnl=_D("100"),
            net_pnl=_D("89.9"),
            pnl_bps=18,
            holding_time_ms=1000,
            notional=_D("5000"),
            outcome="win",
            source="paper",
            dataset_version="v1",
        )
        d = row.to_dict()
        for col in FILL_OUTCOME_COLUMNS:
            assert col in d, f"Missing column: {col}"

    def test_frozen(self) -> None:
        """FillOutcomeRow is immutable."""
        row = FillOutcomeRow(
            row_id="x",
            symbol="BTCUSDT",
            direction="long",
            entry_ts=0,
            entry_price=_D("1"),
            entry_qty=_D("1"),
            entry_fee=_D("0"),
            entry_fill_count=1,
            exit_ts=1,
            exit_price=_D("1"),
            exit_qty=_D("1"),
            exit_fee=_D("0"),
            exit_fill_count=1,
            realized_pnl=_D("0"),
            net_pnl=_D("0"),
            pnl_bps=0,
            holding_time_ms=1,
            notional=_D("1"),
            outcome="breakeven",
            source="paper",
            dataset_version="v1",
        )
        with pytest.raises(AttributeError):
            row.symbol = "ETH"  # type: ignore[misc]


# --- RoundtripTracker ------------------------------------------------------


class TestRoundtripTrackerBasic:
    """REQ-002: Tracker emits FillOutcomeRow when position closes."""

    def test_simple_long_roundtrip(self) -> None:
        """BUY then SELL of same qty emits one row."""
        tracker = RoundtripTracker()
        entry = _fill(1000, "BTCUSDT", "BUY", "50000", "0.1")
        assert tracker.record(entry) is None

        exit_ = _fill(2000, "BTCUSDT", "SELL", "51000", "0.1")
        row = tracker.record(exit_)

        assert row is not None
        assert row.symbol == "BTCUSDT"
        assert row.direction == "long"
        assert row.entry_ts == 1000
        assert row.exit_ts == 2000
        assert row.entry_price == _D("50000")
        assert row.exit_price == _D("51000")
        assert row.entry_qty == _D("0.1")
        assert row.realized_pnl == _D("100")  # (51000-50000)*0.1
        assert row.outcome == "win"
        assert row.holding_time_ms == 1000

    def test_simple_short_roundtrip(self) -> None:
        """SELL then BUY emits a short roundtrip."""
        tracker = RoundtripTracker()
        entry = _fill(1000, "ETHUSDT", "SELL", "3000", "1")
        assert tracker.record(entry) is None

        exit_ = _fill(2000, "ETHUSDT", "BUY", "2900", "1")
        row = tracker.record(exit_)

        assert row is not None
        assert row.direction == "short"
        assert row.realized_pnl == _D("100")  # (3000-2900)*1*1 (short: entry-exit)
        assert row.outcome == "win"

    def test_losing_trade(self) -> None:
        """A trade that loses money."""
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
        row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "49000", "0.1"))

        assert row is not None
        assert row.realized_pnl == _D("-100")  # (49000-50000)*0.1
        assert row.outcome == "loss"

    def test_breakeven_trade(self) -> None:
        """A trade with zero net PnL."""
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
        row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "50000", "0.1"))

        assert row is not None
        assert row.realized_pnl == _D("0")
        assert row.net_pnl == _D("0")
        assert row.outcome == "breakeven"


class TestRoundtripEntryExitDetection:
    """REQ-003: Entry fires on 0->N, exit on N->0, partials don't emit."""

    def test_partial_add_does_not_emit(self) -> None:
        """Adding to existing position does not emit a row."""
        tracker = RoundtripTracker()
        assert tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.05")) is None
        assert tracker.record(_fill(1100, "BTCUSDT", "BUY", "50100", "0.05")) is None

        # Now close the full position
        row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "51000", "0.1"))
        assert row is not None
        assert row.entry_fill_count == 2
        assert row.entry_qty == _D("0.1")
        # Weighted avg entry: (50000*0.05 + 50100*0.05) / 0.1 = 50050
        assert row.entry_price == _D("50050")

    def test_partial_close_does_not_emit(self) -> None:
        """Partial close (not reaching zero) does not emit."""
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
        assert tracker.record(_fill(1500, "BTCUSDT", "SELL", "50500", "0.05")) is None
        # Close remaining
        row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "51000", "0.05"))
        assert row is not None
        assert row.exit_fill_count == 2
        # Weighted avg exit: (50500*0.05 + 51000*0.05) / 0.1 = 50750
        assert row.exit_price == _D("50750")

    def test_multi_symbol_independent(self) -> None:
        """Positions in different symbols are tracked independently."""
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
        tracker.record(_fill(1000, "ETHUSDT", "BUY", "3000", "1"))

        row_eth = tracker.record(_fill(2000, "ETHUSDT", "SELL", "3100", "1"))
        assert row_eth is not None
        assert row_eth.symbol == "ETHUSDT"

        # BTC position still open
        assert ("BTCUSDT", "long") in tracker.open_positions

        row_btc = tracker.record(_fill(3000, "BTCUSDT", "SELL", "51000", "0.1"))
        assert row_btc is not None
        assert row_btc.symbol == "BTCUSDT"

    def test_sequential_roundtrips(self) -> None:
        """Two complete roundtrips in sequence."""
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
        row1 = tracker.record(_fill(2000, "BTCUSDT", "SELL", "51000", "0.1"))
        assert row1 is not None

        tracker.record(_fill(3000, "BTCUSDT", "BUY", "52000", "0.2"))
        row2 = tracker.record(_fill(4000, "BTCUSDT", "SELL", "52500", "0.2"))
        assert row2 is not None
        assert row2.entry_ts == 3000
        assert row2.entry_qty == _D("0.2")


class TestRoundtripFees:
    """Fee handling in roundtrip tracker."""

    def test_fees_reduce_net_pnl(self) -> None:
        """Fees are subtracted from net PnL."""
        tracker = RoundtripTracker()
        tracker.record(
            _fill(1000, "BTCUSDT", "BUY", "50000", "0.1"),
            fee=_D("5"),
        )
        row = tracker.record(
            _fill(2000, "BTCUSDT", "SELL", "51000", "0.1"),
            fee=_D("5.1"),
        )
        assert row is not None
        assert row.entry_fee == _D("5")
        assert row.exit_fee == _D("5.1")
        assert row.realized_pnl == _D("100")
        assert row.net_pnl == _D("89.9")  # 100 - 5 - 5.1

    def test_fees_can_turn_winner_to_loser(self) -> None:
        """Large fees can make a profitable trade into a loss."""
        tracker = RoundtripTracker()
        tracker.record(
            _fill(1000, "BTCUSDT", "BUY", "50000", "0.01"),
            fee=_D("10"),
        )
        row = tracker.record(
            _fill(2000, "BTCUSDT", "SELL", "50100", "0.01"),
            fee=_D("10"),
        )
        assert row is not None
        assert row.realized_pnl == _D("1")  # (50100-50000)*0.01
        assert row.net_pnl == _D("-19")  # 1 - 10 - 10
        assert row.outcome == "loss"


class TestRoundtripPnlBps:
    """pnl_bps calculation."""

    def test_pnl_bps_positive(self) -> None:
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "1"))
        row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "50500", "1"))
        assert row is not None
        # net_pnl = 500, notional = 50000, bps = 500/50000*10000 = 100
        assert row.pnl_bps == 100

    def test_pnl_bps_negative(self) -> None:
        tracker = RoundtripTracker()
        tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "1"))
        row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "49500", "1"))
        assert row is not None
        # net_pnl = -500, notional = 50000, bps = -500/50000*10000 = -100
        assert row.pnl_bps == -100


# --- row_id determinism ----------------------------------------------------


class TestRowIdDeterminism:
    """REQ-004: row_id is deterministic sha1 of canonical fields."""

    def test_same_inputs_same_id(self) -> None:
        id1 = _compute_row_id("BTCUSDT", "long", 1000, 2000, _D("50000"), _D("51000"), _D("0.1"))
        id2 = _compute_row_id("BTCUSDT", "long", 1000, 2000, _D("50000"), _D("51000"), _D("0.1"))
        assert id1 == id2

    def test_different_inputs_different_id(self) -> None:
        id1 = _compute_row_id("BTCUSDT", "long", 1000, 2000, _D("50000"), _D("51000"), _D("0.1"))
        id2 = _compute_row_id("BTCUSDT", "long", 1000, 3000, _D("50000"), _D("51000"), _D("0.1"))
        assert id1 != id2

    def test_id_is_sha1_hex(self) -> None:
        row_id = _compute_row_id("BTCUSDT", "long", 1000, 2000, _D("50000"), _D("51000"), _D("0.1"))
        assert len(row_id) == 40  # sha1 hex length
        int(row_id, 16)  # valid hex

    def test_tracker_produces_deterministic_id(self) -> None:
        """Two identical fill sequences produce the same row_id."""
        rows = []
        for _ in range(2):
            tracker = RoundtripTracker()
            tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
            row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "51000", "0.1"))
            assert row is not None
            rows.append(row)
        assert rows[0].row_id == rows[1].row_id


# --- build_fill_dataset_v1 -------------------------------------------------


def _make_sample_rows() -> list[FillOutcomeRow]:
    """Build a small set of sample rows via tracker."""
    tracker = RoundtripTracker()
    rows = []

    # Roundtrip 1: long win
    tracker.record(_fill(1000, "BTCUSDT", "BUY", "50000", "0.1"))
    row = tracker.record(_fill(2000, "BTCUSDT", "SELL", "51000", "0.1"))
    assert row is not None
    rows.append(row)

    # Roundtrip 2: short win
    tracker.record(_fill(3000, "ETHUSDT", "SELL", "3000", "1"))
    row = tracker.record(_fill(4000, "ETHUSDT", "BUY", "2900", "1"))
    assert row is not None
    rows.append(row)

    # Roundtrip 3: long loss
    tracker.record(_fill(5000, "BTCUSDT", "BUY", "52000", "0.05"))
    row = tracker.record(_fill(6000, "BTCUSDT", "SELL", "51000", "0.05"))
    assert row is not None
    rows.append(row)

    return rows


class TestManifestSchema:
    """REQ-005: Manifest contains required fields."""

    def test_manifest_fields(self, tmp_path: Path) -> None:
        rows = _make_sample_rows()
        dataset_dir = build_fill_dataset_v1(
            rows=rows,
            out_dir=tmp_path,
            created_at_utc="2026-01-01T00:00:00Z",
        )
        manifest = json.loads((dataset_dir / "manifest.json").read_text())

        assert manifest["schema_version"] == "fill_outcomes_v1"
        assert manifest["dataset_id"] == "fill_outcomes_v1"
        assert manifest["created_at_utc"] == "2026-01-01T00:00:00Z"
        assert manifest["row_count"] == 3
        assert "data.parquet" in manifest["sha256"]
        assert manifest["columns"] == list(FILL_OUTCOME_COLUMNS)


class TestArtifactIntegrity:
    """REQ-007: SHA256 in manifest matches actual data.parquet."""

    def test_sha256_matches(self, tmp_path: Path) -> None:
        rows = _make_sample_rows()
        dataset_dir = build_fill_dataset_v1(
            rows=rows,
            out_dir=tmp_path,
            created_at_utc="2026-01-01T00:00:00Z",
        )
        manifest = json.loads((dataset_dir / "manifest.json").read_text())

        # Compute actual sha256
        h = hashlib.sha256()
        with (dataset_dir / "data.parquet").open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        actual = h.hexdigest()

        assert manifest["sha256"]["data.parquet"] == actual


class TestCliBuildFromFixture:
    """REQ-006: CLI script builds from fixture."""

    def test_build_from_fixture(self, tmp_path: Path) -> None:
        # Write a fixture file
        fills_data = [
            {
                "ts": 1000,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "price": "50000",
                "quantity": "0.1",
                "order_id": "ord1",
            },
            {
                "ts": 2000,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "price": "51000",
                "quantity": "0.1",
                "order_id": "ord2",
            },
        ]
        fixture_path = tmp_path / "fills.json"
        fixture_path.write_text(json.dumps(fills_data))

        fills = _load_fills(fixture_path)
        assert len(fills) == 2
        assert fills[0].symbol == "BTCUSDT"

        # Build dataset through tracker + builder
        tracker = RoundtripTracker()
        rows = []
        for fill_obj in fills:
            row = tracker.record(fill_obj)
            if row is not None:
                rows.append(row)

        assert len(rows) == 1

        out_dir = tmp_path / "output"
        dataset_dir = build_fill_dataset_v1(
            rows=rows,
            out_dir=out_dir,
            created_at_utc="2026-01-01T00:00:00Z",
        )

        assert (dataset_dir / "data.parquet").exists()
        assert (dataset_dir / "manifest.json").exists()


class TestDeterminismRebuild:
    """REQ-008: Rebuild from same input produces identical sha256."""

    def test_two_builds_same_sha256(self, tmp_path: Path) -> None:
        rows = _make_sample_rows()

        dir1 = build_fill_dataset_v1(
            rows=rows,
            out_dir=tmp_path / "run1",
            created_at_utc="2026-01-01T00:00:00Z",
        )
        dir2 = build_fill_dataset_v1(
            rows=rows,
            out_dir=tmp_path / "run2",
            created_at_utc="2026-01-01T00:00:00Z",
        )

        m1 = json.loads((dir1 / "manifest.json").read_text())
        m2 = json.loads((dir2 / "manifest.json").read_text())

        assert m1["sha256"]["data.parquet"] == m2["sha256"]["data.parquet"]
        # Manifests should be identical
        assert m1 == m2


class TestEmptyDataset:
    """Edge case: empty fill list produces valid empty dataset."""

    def test_empty_rows(self, tmp_path: Path) -> None:
        dataset_dir = build_fill_dataset_v1(
            rows=[],
            out_dir=tmp_path,
            created_at_utc="2026-01-01T00:00:00Z",
        )
        manifest = json.loads((dataset_dir / "manifest.json").read_text())
        assert manifest["row_count"] == 0
        assert (dataset_dir / "data.parquet").exists()


class TestForceOverwrite:
    """Force flag allows overwriting existing dataset."""

    def test_force_overwrites(self, tmp_path: Path) -> None:
        rows = _make_sample_rows()
        build_fill_dataset_v1(
            rows=rows,
            out_dir=tmp_path,
            created_at_utc="2026-01-01T00:00:00Z",
        )
        # Second build without force should fail
        with pytest.raises(FileExistsError):
            build_fill_dataset_v1(
                rows=rows,
                out_dir=tmp_path,
                created_at_utc="2026-01-01T00:00:00Z",
            )
        # With force should succeed
        dataset_dir = build_fill_dataset_v1(
            rows=rows,
            out_dir=tmp_path,
            force=True,
            created_at_utc="2026-01-01T00:00:00Z",
        )
        assert (dataset_dir / "data.parquet").exists()
