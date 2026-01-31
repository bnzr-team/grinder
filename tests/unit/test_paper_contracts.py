"""Contract tests for paper trading schema stability.

These tests verify that the output schema remains stable and backward-compatible.
Breaking changes to these tests indicate a breaking change to the output contract.

Contract guarantees (v1):
- PaperOutput: ts, symbol, prefilter_result, gating_result, plan, actions,
  events, blocked_by_gating, fills, pnl_snapshot
- PaperResult: schema_version, fixture_path, outputs, digest, events_processed,
  events_gated, orders_placed, orders_blocked, total_fills, final_positions,
  total_realized_pnl, total_unrealized_pnl, errors
- Digest computation is deterministic (same inputs -> same digest)
- All monetary values are strings (Decimal serialization)
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from grinder.paper import (
    SCHEMA_VERSION,
    Fill,
    Ledger,
    PaperEngine,
    PaperOutput,
    PaperResult,
    PnLSnapshot,
    PositionState,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "sample_day"
FIXTURE_ALLOWED_DIR = Path(__file__).parent.parent / "fixtures" / "sample_day_allowed"


class TestSchemaVersion:
    """Tests for schema versioning."""

    def test_schema_version_is_v1(self) -> None:
        """Verify current schema version is v1."""
        assert SCHEMA_VERSION == "v1"

    def test_result_includes_schema_version(self) -> None:
        """Verify PaperResult includes schema_version."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)
        assert result.schema_version == "v1"

    def test_result_dict_has_schema_version(self) -> None:
        """Verify serialized result includes schema_version key."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)
        d = result.to_dict()
        assert "schema_version" in d
        assert d["schema_version"] == "v1"


class TestPaperOutputContract:
    """Tests for PaperOutput schema contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {
        "ts",
        "symbol",
        "prefilter_result",
        "gating_result",
        "plan",
        "actions",
        "events",
        "blocked_by_gating",
        "fills",
        "pnl_snapshot",
    }

    def test_output_has_all_required_keys(self) -> None:
        """Verify output dict contains all required keys."""
        output = PaperOutput(
            ts=1000,
            symbol="BTCUSDT",
            prefilter_result={"allowed": True, "reason": "PASS"},
            gating_result={"allowed": True, "reason": "PASS"},
            plan={"mode": "BI_LATERAL"},
            actions=[],
            events=[],
            blocked_by_gating=False,
            fills=[],
            pnl_snapshot={"ts": 1000, "realized_pnl": "0", "unrealized_pnl": "0"},
        )
        d = output.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing required keys: {missing}"

    def test_output_from_engine_has_all_keys(self) -> None:
        """Verify real engine output contains all required keys."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_ALLOWED_DIR)
        assert len(result.outputs) > 0
        for output in result.outputs:
            d = output.to_dict()
            missing = self.REQUIRED_KEYS - set(d.keys())
            assert not missing, f"Missing keys in output: {missing}"

    def test_fills_is_list(self) -> None:
        """Verify fills field is always a list."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)
        for output in result.outputs:
            assert isinstance(output.fills, list)

    def test_pnl_snapshot_is_dict_or_none(self) -> None:
        """Verify pnl_snapshot is dict or None."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)
        for output in result.outputs:
            assert output.pnl_snapshot is None or isinstance(output.pnl_snapshot, dict)


class TestPaperResultContract:
    """Tests for PaperResult schema contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {
        "schema_version",
        "fixture_path",
        "outputs",
        "digest",
        "events_processed",
        "events_gated",
        "orders_placed",
        "orders_blocked",
        "total_fills",
        "final_positions",
        "total_realized_pnl",
        "total_unrealized_pnl",
        "errors",
    }

    def test_result_has_all_required_keys(self) -> None:
        """Verify result dict contains all required keys."""
        result = PaperResult(
            fixture_path="/test",
            digest="abc123",
            events_processed=5,
        )
        d = result.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing required keys: {missing}"

    def test_result_from_engine_has_all_keys(self) -> None:
        """Verify real engine result contains all required keys."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_ALLOWED_DIR)
        d = result.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing keys in result: {missing}"

    def test_pnl_values_are_strings(self) -> None:
        """Verify PnL values are serialized as strings (Decimal)."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)
        d = result.to_dict()
        assert isinstance(d["total_realized_pnl"], str)
        assert isinstance(d["total_unrealized_pnl"], str)

    def test_final_positions_is_dict(self) -> None:
        """Verify final_positions is a dict."""
        engine = PaperEngine()
        result = engine.run(FIXTURE_DIR)
        assert isinstance(result.final_positions, dict)


class TestFillContract:
    """Tests for Fill data class contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {"ts", "symbol", "side", "price", "quantity", "order_id"}

    def test_fill_has_all_required_keys(self) -> None:
        """Verify Fill.to_dict() has all required keys."""
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            order_id="order_123",
        )
        d = fill.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_fill_prices_are_strings(self) -> None:
        """Verify Fill serializes Decimals as strings."""
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.123"),
            quantity=Decimal("0.01234"),
            order_id="order_123",
        )
        d = fill.to_dict()
        assert isinstance(d["price"], str)
        assert isinstance(d["quantity"], str)

    def test_fill_roundtrip(self) -> None:
        """Verify Fill can roundtrip through dict."""
        original = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000.123"),
            quantity=Decimal("0.01234"),
            order_id="order_123",
        )
        d = original.to_dict()
        restored = Fill.from_dict(d)
        assert restored == original


class TestPnLSnapshotContract:
    """Tests for PnLSnapshot data class contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {
        "ts",
        "symbol",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
    }

    def test_pnl_snapshot_has_all_required_keys(self) -> None:
        """Verify PnLSnapshot.to_dict() has all required keys."""
        snap = PnLSnapshot(
            ts=1000,
            symbol="BTCUSDT",
            realized_pnl=Decimal("10.5"),
            unrealized_pnl=Decimal("-5.25"),
            total_pnl=Decimal("5.25"),
        )
        d = snap.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_pnl_values_are_strings(self) -> None:
        """Verify PnLSnapshot serializes Decimals as strings."""
        snap = PnLSnapshot(
            ts=1000,
            symbol="BTCUSDT",
            realized_pnl=Decimal("10.5"),
            unrealized_pnl=Decimal("-5.25"),
            total_pnl=Decimal("5.25"),
        )
        d = snap.to_dict()
        assert isinstance(d["realized_pnl"], str)
        assert isinstance(d["unrealized_pnl"], str)
        assert isinstance(d["total_pnl"], str)


class TestPositionStateContract:
    """Tests for PositionState data class contract."""

    REQUIRED_KEYS: ClassVar[set[str]] = {"quantity", "avg_entry_price", "realized_pnl"}

    def test_position_state_has_all_required_keys(self) -> None:
        """Verify PositionState.to_dict() has all required keys."""
        pos = PositionState(
            quantity=Decimal("1.5"),
            avg_entry_price=Decimal("50000"),
            realized_pnl=Decimal("100"),
        )
        d = pos.to_dict()
        missing = self.REQUIRED_KEYS - set(d.keys())
        assert not missing, f"Missing keys: {missing}"


class TestLedgerContract:
    """Tests for Ledger behavior contract."""

    def test_ledger_tracks_position_after_fill(self) -> None:
        """Verify ledger updates position after applying fill."""
        ledger = Ledger()
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("1"),
            order_id="order_1",
        )
        ledger.apply_fill(fill)
        pos = ledger.get_position("BTCUSDT")
        assert pos.quantity == Decimal("1")
        assert pos.avg_entry_price == Decimal("50000")

    def test_ledger_computes_unrealized_pnl(self) -> None:
        """Verify ledger computes unrealized PnL correctly."""
        ledger = Ledger()
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("1"),
            order_id="order_1",
        )
        ledger.apply_fill(fill)

        # Price goes up -> profit
        unrealized = ledger.get_unrealized_pnl("BTCUSDT", Decimal("51000"))
        assert unrealized == Decimal("1000")

        # Price goes down -> loss
        unrealized = ledger.get_unrealized_pnl("BTCUSDT", Decimal("49000"))
        assert unrealized == Decimal("-1000")

    def test_ledger_realizes_pnl_on_close(self) -> None:
        """Verify ledger realizes PnL when closing position."""
        ledger = Ledger()

        # Open long
        buy = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("1"),
            order_id="order_1",
        )
        ledger.apply_fill(buy)

        # Close at profit
        sell = Fill(
            ts=2000,
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("51000"),
            quantity=Decimal("1"),
            order_id="order_2",
        )
        ledger.apply_fill(sell)

        pos = ledger.get_position("BTCUSDT")
        assert pos.quantity == Decimal("0")
        assert pos.realized_pnl == Decimal("1000")

    def test_ledger_reset_clears_state(self) -> None:
        """Verify reset clears all ledger state."""
        ledger = Ledger()
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("1"),
            order_id="order_1",
        )
        ledger.apply_fill(fill)

        ledger.reset()

        pos = ledger.get_position("BTCUSDT")
        assert pos.quantity == Decimal("0")
        assert ledger.get_total_realized_pnl() == Decimal("0")


class TestDeterminism:
    """Tests for output determinism."""

    def test_same_fixture_produces_same_digest(self) -> None:
        """Verify same fixture produces identical digest across runs."""
        digests = []
        for _ in range(3):
            engine = PaperEngine()
            result = engine.run(FIXTURE_ALLOWED_DIR)
            digests.append(result.digest)

        assert len(set(digests)) == 1, f"Digests differ: {digests}"

    def test_same_params_produce_same_result(self) -> None:
        """Verify identical params produce identical results."""
        results = []
        for _ in range(2):
            engine = PaperEngine(
                spacing_bps=15.0,
                levels=3,
                size_per_level=Decimal("50"),
            )
            result = engine.run(FIXTURE_ALLOWED_DIR)
            results.append(result.to_json())

        assert results[0] == results[1], "Results differ between runs"

    def test_different_params_produce_different_digest(self) -> None:
        """Verify different params produce different digests."""
        engine1 = PaperEngine(spacing_bps=10.0, levels=5)
        result1 = engine1.run(FIXTURE_ALLOWED_DIR)

        engine2 = PaperEngine(spacing_bps=20.0, levels=3)
        result2 = engine2.run(FIXTURE_ALLOWED_DIR)

        assert result1.digest != result2.digest


class TestPnLInvariants:
    """Tests for PnL calculation invariants."""

    def test_total_pnl_equals_realized_plus_unrealized(self) -> None:
        """Verify total = realized + unrealized."""
        ledger = Ledger()

        # Open position
        fill = Fill(
            ts=1000,
            symbol="BTCUSDT",
            side="BUY",
            price=Decimal("50000"),
            quantity=Decimal("1"),
            order_id="order_1",
        )
        ledger.apply_fill(fill)

        current_price = Decimal("51500")
        snap = ledger.get_pnl_snapshot(1000, "BTCUSDT", current_price)

        assert snap.total_pnl == snap.realized_pnl + snap.unrealized_pnl

    def test_flat_position_has_zero_unrealized(self) -> None:
        """Verify flat position has zero unrealized PnL."""
        ledger = Ledger()

        # Open and close
        ledger.apply_fill(
            Fill(
                ts=1000,
                symbol="BTCUSDT",
                side="BUY",
                price=Decimal("50000"),
                quantity=Decimal("1"),
                order_id="order_1",
            )
        )
        ledger.apply_fill(
            Fill(
                ts=2000,
                symbol="BTCUSDT",
                side="SELL",
                price=Decimal("51000"),
                quantity=Decimal("1"),
                order_id="order_2",
            )
        )

        unrealized = ledger.get_unrealized_pnl("BTCUSDT", Decimal("52000"))
        assert unrealized == Decimal("0")

    def test_no_position_no_pnl(self) -> None:
        """Verify no position means no unrealized PnL."""
        ledger = Ledger()
        unrealized = ledger.get_unrealized_pnl("BTCUSDT", Decimal("50000"))
        assert unrealized == Decimal("0")
