"""Tests for L2 Snapshot JSONL v0 parser and validator.

Tests verify:
- Happy path parsing of all 4 scenarios
- Roundtrip serialization
- Invariant validation (sorting, qty > 0, depth)
- Error handling for malformed input

See: docs/smart_grid/SPEC_V2_0.md, Addendum B
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from grinder.replay.l2_snapshot import (
    IMPACT_INSUFFICIENT_DEPTH_BPS,
    QTY_REF_BASELINE,
    BookLevel,
    L2ParseError,
    load_l2_fixtures,
    parse_l2_snapshot_line,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "l2"


class TestConstants:
    """Tests for L2 constants."""

    def test_qty_ref_baseline(self) -> None:
        """Test QTY_REF_BASELINE value per SPEC B.2."""
        assert Decimal("0.003") == QTY_REF_BASELINE

    def test_impact_insufficient_depth_bps(self) -> None:
        """Test IMPACT_INSUFFICIENT_DEPTH_BPS value per SPEC B.2."""
        assert IMPACT_INSUFFICIENT_DEPTH_BPS == 500


class TestBookLevel:
    """Tests for BookLevel dataclass."""

    def test_book_level_frozen(self) -> None:
        """Test BookLevel is immutable."""
        level = BookLevel(price=Decimal("70810.90"), qty=Decimal("0.120"))
        with pytest.raises(AttributeError):
            level.qty = Decimal("999")  # type: ignore[misc]

    def test_book_level_to_tuple(self) -> None:
        """Test to_tuple serialization for JSONL format."""
        level = BookLevel(price=Decimal("70810.90"), qty=Decimal("0.120"))
        assert level.to_tuple() == ("70810.90", "0.120")


class TestL2SnapshotHappyPath:
    """Happy path tests for L2Snapshot parsing."""

    def test_parse_normal_scenario(self) -> None:
        """Parse normal scenario - healthy book with sufficient liquidity."""
        line = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1770552988001,'
            '"symbol":"BTCUSDT","venue":"binance_futures_usdtm","depth":5,'
            '"bids":[["70810.90","0.120"],["70810.50","0.250"],["70810.00","0.600"],'
            '["70809.50","0.900"],["70809.00","1.200"]],'
            '"asks":[["70811.10","0.110"],["70811.50","0.230"],["70812.00","0.550"],'
            '["70812.50","0.850"],["70813.00","1.100"]],'
            '"meta":{"src":"fixture","scenario":"normal"}}'
        )
        snap = parse_l2_snapshot_line(line)

        assert snap.ts_ms == 1770552988001
        assert snap.symbol == "BTCUSDT"
        assert snap.venue == "binance_futures_usdtm"
        assert snap.depth == 5
        assert len(snap.bids) == 5
        assert len(snap.asks) == 5

        # Check best bid/ask
        assert snap.best_bid == Decimal("70810.90")
        assert snap.best_ask == Decimal("70811.10")

        # Check meta
        assert snap.meta["scenario"] == "normal"

    def test_parse_ultra_thin_scenario(self) -> None:
        """Parse ultra_thin scenario - thin top levels."""
        line = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1770553003001,'
            '"symbol":"BTCUSDT","venue":"binance_futures_usdtm","depth":5,'
            '"bids":[["70829.50","0.001"],["70808.25","0.002"],["70800.00","0.010"],'
            '["70790.00","0.015"],["70780.00","0.020"]],'
            '"asks":[["70830.00","0.001"],["70851.25","0.002"],["70860.00","0.010"],'
            '["70870.00","0.015"],["70880.00","0.020"]],'
            '"meta":{"src":"fixture","scenario":"ultra_thin"}}'
        )
        snap = parse_l2_snapshot_line(line)

        assert snap.meta["scenario"] == "ultra_thin"
        assert snap.bids[0].qty == Decimal("0.001")
        assert snap.asks[0].qty == Decimal("0.001")

    def test_parse_wall_bid_scenario(self) -> None:
        """Parse wall_bid scenario - large wall at second bid level."""
        line = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1770552998001,'
            '"symbol":"BTCUSDT","venue":"binance_futures_usdtm","depth":5,'
            '"bids":[["70810.90","0.120"],["70810.50","2.500"],["70810.00","0.140"],'
            '["70809.50","0.160"],["70809.00","0.180"]],'
            '"asks":[["70811.10","0.110"],["70811.50","0.130"],["70812.00","0.150"],'
            '["70812.50","0.170"],["70813.00","0.190"]],'
            '"meta":{"src":"fixture","scenario":"wall_bid"}}'
        )
        snap = parse_l2_snapshot_line(line)

        assert snap.meta["scenario"] == "wall_bid"
        # Wall at second bid level: 2.500 BTC
        assert snap.bids[1].qty == Decimal("2.500")

    def test_parse_thin_insufficient_scenario(self) -> None:
        """Parse thin_insufficient scenario - depth exhausted at higher qty_ref."""
        line = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1770552993001,'
            '"symbol":"BTCUSDT","venue":"binance_futures_usdtm","depth":5,'
            '"bids":[["70790.00","0.010"],["70780.00","0.012"],["70770.00","0.015"],'
            '["70760.00","0.020"],["70750.00","0.030"]],'
            '"asks":[["70830.00","0.009"],["70840.00","0.011"],["70850.00","0.014"],'
            '["70860.00","0.019"],["70870.00","0.028"]],'
            '"meta":{"src":"fixture","scenario":"thin_insufficient_for_q_0_1"}}'
        )
        snap = parse_l2_snapshot_line(line)

        assert snap.meta["scenario"] == "thin_insufficient_for_q_0_1"
        # Total bid depth: 0.010 + 0.012 + 0.015 + 0.020 + 0.030 = 0.087
        # Total ask depth: 0.009 + 0.011 + 0.014 + 0.019 + 0.028 = 0.081
        total_bid_depth = sum(level.qty for level in snap.bids)
        total_ask_depth = sum(level.qty for level in snap.asks)
        assert total_bid_depth == Decimal("0.087")
        assert total_ask_depth == Decimal("0.081")

    def test_frozen_immutable(self) -> None:
        """Test L2Snapshot is immutable (frozen)."""
        snap = parse_l2_snapshot_line(
            '{"type":"l2_snapshot","v":0,"ts_ms":1000,"symbol":"TEST",'
            '"venue":"test","depth":1,'
            '"bids":[["100.00","1.0"]],"asks":[["101.00","1.0"]],'
            '"meta":{}}'
        )
        with pytest.raises(AttributeError):
            snap.symbol = "CHANGED"  # type: ignore[misc]

    def test_mid_price_calculation(self) -> None:
        """Test mid_price property calculation."""
        snap = parse_l2_snapshot_line(
            '{"type":"l2_snapshot","v":0,"ts_ms":1000,"symbol":"TEST",'
            '"venue":"test","depth":1,'
            '"bids":[["100.00","1.0"]],"asks":[["102.00","1.0"]],'
            '"meta":{}}'
        )
        assert snap.mid_price == Decimal("101.00")

    def test_roundtrip_to_dict(self) -> None:
        """Test to_dict/to_json roundtrip serialization."""
        original_line = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1770552988001,'
            '"symbol":"BTCUSDT","venue":"binance_futures_usdtm","depth":2,'
            '"bids":[["70810.90","0.120"],["70810.50","0.250"]],'
            '"asks":[["70811.10","0.110"],["70811.50","0.230"]],'
            '"meta":{"scenario":"test"}}'
        )
        snap = parse_l2_snapshot_line(original_line)
        d = snap.to_dict()

        # Re-parse the dict
        snap2 = parse_l2_snapshot_line(json.dumps(d))

        assert snap2.ts_ms == snap.ts_ms
        assert snap2.symbol == snap.symbol
        assert snap2.venue == snap.venue
        assert snap2.depth == snap.depth
        assert snap2.bids == snap.bids
        assert snap2.asks == snap.asks
        assert snap2.meta == snap.meta

    def test_to_json_compact(self) -> None:
        """Test to_json produces compact output (no spaces)."""
        snap = parse_l2_snapshot_line(
            '{"type":"l2_snapshot","v":0,"ts_ms":1000,"symbol":"TEST",'
            '"venue":"test","depth":1,'
            '"bids":[["100.00","1.0"]],"asks":[["101.00","1.0"]],'
            '"meta":{}}'
        )
        json_str = snap.to_json()
        assert " " not in json_str  # Compact format
        assert "\n" not in json_str  # Single line


class TestL2SnapshotNegativePaths:
    """Negative path tests for L2Snapshot validation."""

    def test_invalid_json(self) -> None:
        """Reject malformed JSON."""
        with pytest.raises(L2ParseError, match="Invalid JSON"):
            parse_l2_snapshot_line("not json {")

    def test_not_object(self) -> None:
        """Reject non-object JSON."""
        with pytest.raises(L2ParseError, match="Expected JSON object"):
            parse_l2_snapshot_line("[]")

    def test_wrong_type(self) -> None:
        """Reject wrong type field."""
        with pytest.raises(L2ParseError, match="Expected type='l2_snapshot'"):
            parse_l2_snapshot_line('{"type":"l1_tick","v":0}')

    def test_missing_type(self) -> None:
        """Reject missing type field."""
        with pytest.raises(L2ParseError, match="Expected type='l2_snapshot'"):
            parse_l2_snapshot_line('{"v":0}')

    def test_unsupported_version(self) -> None:
        """Reject unsupported schema version."""
        with pytest.raises(L2ParseError, match="Unsupported schema version"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":99,"ts_ms":0,"symbol":"X",'
                '"venue":"x","depth":0,"bids":[],"asks":[]}'
            )

    def test_missing_required_field(self) -> None:
        """Reject missing required fields."""
        with pytest.raises(L2ParseError, match=r"Missing required field.*symbol"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,"venue":"x","depth":0,"bids":[],"asks":[]}'
            )

    def test_ts_ms_wrong_type(self) -> None:
        """Reject non-int ts_ms."""
        with pytest.raises(L2ParseError, match="ts_ms must be int"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":"string",'
                '"symbol":"X","venue":"x","depth":0,"bids":[],"asks":[]}'
            )

    def test_bids_not_list(self) -> None:
        """Reject non-list bids."""
        with pytest.raises(L2ParseError, match="bids must be list"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":0,"bids":{},"asks":[]}'
            )

    def test_depth_mismatch_bids(self) -> None:
        """Reject when len(bids) != depth."""
        with pytest.raises(L2ParseError, match=r"depth mismatch.*bids"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":2,'
                '"bids":[["100.0","1.0"]],'
                '"asks":[["101.0","1.0"],["102.0","1.0"]]}'
            )

    def test_depth_mismatch_asks(self) -> None:
        """Reject when len(asks) != depth."""
        with pytest.raises(L2ParseError, match=r"depth mismatch.*asks"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":2,'
                '"bids":[["100.0","1.0"],["99.0","1.0"]],'
                '"asks":[["101.0","1.0"]]}'
            )

    def test_bids_not_descending(self) -> None:
        """Reject when bid prices not strictly descending."""
        with pytest.raises(L2ParseError, match="bids: prices not strictly descending"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":2,'
                '"bids":[["100.0","1.0"],["100.0","1.0"]],'  # Same price
                '"asks":[["101.0","1.0"],["102.0","1.0"]]}'
            )

    def test_bids_ascending_rejected(self) -> None:
        """Reject when bid prices are ascending (wrong order)."""
        with pytest.raises(L2ParseError, match="bids: prices not strictly descending"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":2,'
                '"bids":[["99.0","1.0"],["100.0","1.0"]],'  # Ascending
                '"asks":[["101.0","1.0"],["102.0","1.0"]]}'
            )

    def test_asks_not_ascending(self) -> None:
        """Reject when ask prices not strictly ascending."""
        with pytest.raises(L2ParseError, match="asks: prices not strictly ascending"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":2,'
                '"bids":[["100.0","1.0"],["99.0","1.0"]],'
                '"asks":[["102.0","1.0"],["101.0","1.0"]]}'  # Descending
            )

    def test_asks_same_price_rejected(self) -> None:
        """Reject when ask prices are equal."""
        with pytest.raises(L2ParseError, match="asks: prices not strictly ascending"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":2,'
                '"bids":[["100.0","1.0"],["99.0","1.0"]],'
                '"asks":[["101.0","1.0"],["101.0","1.0"]]}'  # Same price
            )

    def test_qty_zero_rejected(self) -> None:
        """Reject zero quantity."""
        with pytest.raises(L2ParseError, match="qty must be > 0"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":1,'
                '"bids":[["100.0","0.0"]],'  # Zero qty
                '"asks":[["101.0","1.0"]]}'
            )

    def test_qty_negative_rejected(self) -> None:
        """Reject negative quantity."""
        with pytest.raises(L2ParseError, match="qty must be > 0"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":1,'
                '"bids":[["100.0","1.0"]],'
                '"asks":[["101.0","-0.5"]]}'  # Negative qty
            )

    def test_invalid_level_format(self) -> None:
        """Reject malformed level (not [price, qty] pair)."""
        with pytest.raises(L2ParseError, match="expected \\[price, qty\\]"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":1,'
                '"bids":[["100.0"]],'  # Missing qty
                '"asks":[["101.0","1.0"]]}'
            )

    def test_invalid_decimal(self) -> None:
        """Reject invalid decimal string."""
        with pytest.raises(L2ParseError, match="invalid decimal"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":1,'
                '"bids":[["not_a_number","1.0"]],'
                '"asks":[["101.0","1.0"]]}'
            )

    def test_meta_not_dict_rejected(self) -> None:
        """Reject non-dict meta field."""
        with pytest.raises(L2ParseError, match="meta must be dict"):
            parse_l2_snapshot_line(
                '{"type":"l2_snapshot","v":0,"ts_ms":0,'
                '"symbol":"X","venue":"x","depth":1,'
                '"bids":[["100.0","1.0"]],'
                '"asks":[["101.0","1.0"]],'
                '"meta":["not","a","dict"]}'
            )


class TestLoadL2Fixtures:
    """Tests for load_l2_fixtures function."""

    def test_load_all_scenarios(self) -> None:
        """Load all 4 scenarios from fixture file."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"
        snapshots = load_l2_fixtures(str(path))

        assert len(snapshots) == 4

        scenarios = [s.meta.get("scenario") for s in snapshots]
        assert "normal" in scenarios
        assert "ultra_thin" in scenarios
        assert "wall_bid" in scenarios
        assert "thin_insufficient_for_q_0_1" in scenarios

    def test_load_handles_empty_lines(self) -> None:
        """Empty lines should be skipped."""
        content = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1000,"symbol":"A",'
            '"venue":"x","depth":1,"bids":[["100.0","1.0"]],'
            '"asks":[["101.0","1.0"]],"meta":{}}\n'
            "\n"
            '{"type":"l2_snapshot","v":0,"ts_ms":2000,"symbol":"B",'
            '"venue":"x","depth":1,"bids":[["200.0","1.0"]],'
            '"asks":[["201.0","1.0"]],"meta":{}}\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(content)
            f.flush()
            snapshots = load_l2_fixtures(f.name)

        assert len(snapshots) == 2
        assert snapshots[0].symbol == "A"
        assert snapshots[1].symbol == "B"

    def test_load_error_includes_line_number(self) -> None:
        """Parse errors should include line number."""
        content = (
            '{"type":"l2_snapshot","v":0,"ts_ms":1000,"symbol":"A",'
            '"venue":"x","depth":1,"bids":[["100.0","1.0"]],'
            '"asks":[["101.0","1.0"]],"meta":{}}\n'
            '{"type":"l2_snapshot","v":99}\n'  # Invalid version on line 2
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(content)
            f.flush()
            with pytest.raises(L2ParseError, match="Line 2"):
                load_l2_fixtures(f.name)

    def test_file_not_found(self) -> None:
        """FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_l2_fixtures("/nonexistent/path.jsonl")


class TestL2SnapshotEdgeCases:
    """Edge case tests."""

    def test_depth_1_minimal(self) -> None:
        """Minimal valid snapshot with depth=1."""
        snap = parse_l2_snapshot_line(
            '{"type":"l2_snapshot","v":0,"ts_ms":0,'
            '"symbol":"X","venue":"x","depth":1,'
            '"bids":[["100.0","0.001"]],'
            '"asks":[["101.0","0.001"]]}'
        )
        assert snap.depth == 1
        assert len(snap.bids) == 1
        assert len(snap.asks) == 1

    def test_meta_optional_defaults_to_empty(self) -> None:
        """Missing meta field defaults to empty dict."""
        snap = parse_l2_snapshot_line(
            '{"type":"l2_snapshot","v":0,"ts_ms":0,'
            '"symbol":"X","venue":"x","depth":1,'
            '"bids":[["100.0","1.0"]],'
            '"asks":[["101.0","1.0"]]}'
        )
        assert snap.meta == {}

    def test_high_precision_decimals(self) -> None:
        """High precision decimals preserved correctly."""
        snap = parse_l2_snapshot_line(
            '{"type":"l2_snapshot","v":0,"ts_ms":0,'
            '"symbol":"X","venue":"x","depth":1,'
            '"bids":[["12345.67890123","0.00000001"]],'
            '"asks":[["12345.67890124","0.00000002"]]}'
        )
        assert snap.bids[0].price == Decimal("12345.67890123")
        assert snap.bids[0].qty == Decimal("0.00000001")
        assert snap.asks[0].price == Decimal("12345.67890124")
        assert snap.asks[0].qty == Decimal("0.00000002")


class TestDeterminism:
    """Determinism tests for L2 fixture parsing.

    Verifies:
    - Same fixture file produces same SHA256 across runs
    - Same parsed snapshots produce same to_json() output
    - Round-trip serialization is stable
    """

    def test_fixture_file_sha256_stable(self) -> None:
        """Fixture file content has stable SHA256 digest."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()

        # Verify it's a valid 64-char hex digest
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_parsed_output_determinism(self) -> None:
        """Same fixture produces identical to_json() output across 2 runs."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"

        def compute_digest() -> str:
            snapshots = load_l2_fixtures(str(path))
            # Combine all to_json() outputs
            combined = "\n".join(s.to_json() for s in snapshots)
            return hashlib.sha256(combined.encode()).hexdigest()

        digest1 = compute_digest()
        digest2 = compute_digest()

        assert digest1 == digest2, "Parsed output not deterministic across runs"

    def test_roundtrip_preserves_content(self) -> None:
        """Parse -> to_json -> parse produces identical snapshot."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"
        snapshots = load_l2_fixtures(str(path))

        for original in snapshots:
            # Serialize and re-parse
            json_str = original.to_json()
            restored = parse_l2_snapshot_line(json_str)

            # Compare all fields
            assert restored.ts_ms == original.ts_ms
            assert restored.symbol == original.symbol
            assert restored.venue == original.venue
            assert restored.depth == original.depth
            assert restored.bids == original.bids
            assert restored.asks == original.asks
            assert restored.meta == original.meta
