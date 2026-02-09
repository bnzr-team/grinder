"""Unit tests for L2 feature indicators and L2FeatureSnapshot.

Tests L2 feature computation per SPEC_V2_0.md Addendum B:
- Impact-Lite (VWAP slippage) §B.3
- Wall Score §B.4
- Depth imbalance

Uses 4 fixture scenarios: normal, ultra_thin, wall_bid, thin_insufficient

See: docs/smart_grid/SPEC_V2_0.md, Addendum B
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from grinder.features.l2_indicators import (
    compute_depth_imbalance_bps,
    compute_depth_totals,
    compute_impact_buy_bps,
    compute_impact_sell_bps,
    compute_wall_score_x1000,
)
from grinder.features.l2_types import L2FeatureSnapshot
from grinder.replay.l2_snapshot import (
    IMPACT_INSUFFICIENT_DEPTH_BPS,
    QTY_REF_BASELINE,
    BookLevel,
    L2Snapshot,
    load_l2_fixtures,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "l2"


class TestDepthTotals:
    """Tests for compute_depth_totals."""

    def test_empty_book(self) -> None:
        """Empty book returns zeros."""
        bid_total, ask_total = compute_depth_totals((), ())
        assert bid_total == Decimal("0")
        assert ask_total == Decimal("0")

    def test_single_level(self) -> None:
        """Single level returns that qty."""
        bids = (BookLevel(price=Decimal("100"), qty=Decimal("1.5")),)
        asks = (BookLevel(price=Decimal("101"), qty=Decimal("2.5")),)
        bid_total, ask_total = compute_depth_totals(bids, asks)
        assert bid_total == Decimal("1.5")
        assert ask_total == Decimal("2.5")

    def test_multi_level(self) -> None:
        """Multiple levels sum correctly."""
        bids = (
            BookLevel(price=Decimal("100"), qty=Decimal("1")),
            BookLevel(price=Decimal("99"), qty=Decimal("2")),
            BookLevel(price=Decimal("98"), qty=Decimal("3")),
        )
        asks = (
            BookLevel(price=Decimal("101"), qty=Decimal("0.5")),
            BookLevel(price=Decimal("102"), qty=Decimal("1.5")),
        )
        bid_total, ask_total = compute_depth_totals(bids, asks)
        assert bid_total == Decimal("6")
        assert ask_total == Decimal("2")


class TestDepthImbalance:
    """Tests for compute_depth_imbalance_bps."""

    def test_balanced_book(self) -> None:
        """Equal depth returns ~0."""
        bids = (BookLevel(price=Decimal("100"), qty=Decimal("1")),)
        asks = (BookLevel(price=Decimal("101"), qty=Decimal("1")),)
        imbalance = compute_depth_imbalance_bps(bids, asks)
        assert imbalance == 0

    def test_bid_heavy(self) -> None:
        """More bids returns positive."""
        bids = (BookLevel(price=Decimal("100"), qty=Decimal("3")),)
        asks = (BookLevel(price=Decimal("101"), qty=Decimal("1")),)
        imbalance = compute_depth_imbalance_bps(bids, asks)
        # (3 - 1) / (3 + 1) = 0.5 = 5000 bps
        assert imbalance == 5000

    def test_ask_heavy(self) -> None:
        """More asks returns negative."""
        bids = (BookLevel(price=Decimal("100"), qty=Decimal("1")),)
        asks = (BookLevel(price=Decimal("101"), qty=Decimal("3")),)
        imbalance = compute_depth_imbalance_bps(bids, asks)
        # (1 - 3) / (1 + 3) = -0.5 = -5000 bps
        assert imbalance == -5000

    def test_empty_book(self) -> None:
        """Empty book returns 0."""
        imbalance = compute_depth_imbalance_bps((), ())
        assert imbalance == 0


class TestImpactBuy:
    """Tests for compute_impact_buy_bps."""

    def test_empty_asks(self) -> None:
        """Empty asks returns INSUFFICIENT."""
        impact = compute_impact_buy_bps(())
        assert impact == IMPACT_INSUFFICIENT_DEPTH_BPS

    def test_fits_in_top_level(self) -> None:
        """Qty fits in top level returns 0 bps."""
        asks = (
            BookLevel(price=Decimal("100"), qty=Decimal("1")),
            BookLevel(price=Decimal("101"), qty=Decimal("2")),
        )
        # qty_ref = 0.003, top level has 1.0 > 0.003
        impact = compute_impact_buy_bps(asks, Decimal("0.003"))
        assert impact == 0

    def test_walks_book(self) -> None:
        """Walking book gives positive slippage."""
        # Top level: 0.001 @ 100, need 0.002 more @ 101
        # VWAP = (0.001 * 100 + 0.002 * 101) / 0.003 = (0.1 + 0.202) / 0.003 = 100.666...
        # slippage = (100.666 - 100) / 100 * 10000 = ~66 bps
        asks = (
            BookLevel(price=Decimal("100"), qty=Decimal("0.001")),
            BookLevel(price=Decimal("101"), qty=Decimal("0.002")),
        )
        impact = compute_impact_buy_bps(asks, Decimal("0.003"))
        assert impact == 67  # round(66.666...)

    def test_insufficient_depth(self) -> None:
        """Insufficient depth returns IMPACT_INSUFFICIENT_DEPTH_BPS."""
        asks = (
            BookLevel(price=Decimal("100"), qty=Decimal("0.001")),
            BookLevel(price=Decimal("101"), qty=Decimal("0.001")),
        )
        impact = compute_impact_buy_bps(asks, Decimal("0.1"))  # Need 0.1, have 0.002
        assert impact == IMPACT_INSUFFICIENT_DEPTH_BPS


class TestImpactSell:
    """Tests for compute_impact_sell_bps."""

    def test_empty_bids(self) -> None:
        """Empty bids returns INSUFFICIENT."""
        impact = compute_impact_sell_bps(())
        assert impact == IMPACT_INSUFFICIENT_DEPTH_BPS

    def test_fits_in_top_level(self) -> None:
        """Qty fits in top level returns 0 bps."""
        bids = (
            BookLevel(price=Decimal("100"), qty=Decimal("1")),
            BookLevel(price=Decimal("99"), qty=Decimal("2")),
        )
        impact = compute_impact_sell_bps(bids, Decimal("0.003"))
        assert impact == 0

    def test_walks_book(self) -> None:
        """Walking book gives positive slippage."""
        # Top level: 0.001 @ 100, need 0.002 more @ 99
        # VWAP = (0.001 * 100 + 0.002 * 99) / 0.003 = (0.1 + 0.198) / 0.003 = 99.333...
        # slippage = (100 - 99.333) / 100 * 10000 = ~67 bps
        bids = (
            BookLevel(price=Decimal("100"), qty=Decimal("0.001")),
            BookLevel(price=Decimal("99"), qty=Decimal("0.002")),
        )
        impact = compute_impact_sell_bps(bids, Decimal("0.003"))
        assert impact == 67

    def test_insufficient_depth(self) -> None:
        """Insufficient depth returns IMPACT_INSUFFICIENT_DEPTH_BPS."""
        bids = (
            BookLevel(price=Decimal("100"), qty=Decimal("0.001")),
            BookLevel(price=Decimal("99"), qty=Decimal("0.001")),
        )
        impact = compute_impact_sell_bps(bids, Decimal("0.1"))
        assert impact == IMPACT_INSUFFICIENT_DEPTH_BPS


class TestWallScore:
    """Tests for compute_wall_score_x1000."""

    def test_less_than_3_levels(self) -> None:
        """< 3 levels returns 1000 (default)."""
        levels = (
            BookLevel(price=Decimal("100"), qty=Decimal("1")),
            BookLevel(price=Decimal("99"), qty=Decimal("2")),
        )
        score = compute_wall_score_x1000(levels)
        assert score == 1000

    def test_no_wall(self) -> None:
        """Uniform quantities = ratio ~1."""
        levels = (
            BookLevel(price=Decimal("100"), qty=Decimal("1")),
            BookLevel(price=Decimal("99"), qty=Decimal("1")),
            BookLevel(price=Decimal("98"), qty=Decimal("1")),
        )
        score = compute_wall_score_x1000(levels)
        assert score == 1000

    def test_clear_wall(self) -> None:
        """One large order = high ratio."""
        # quantities: 0.1, 0.1, 10.0 (sorted: 0.1, 0.1, 10.0)
        # median = 0.1, max = 10, ratio = 100 -> 100000 x1000
        levels = (
            BookLevel(price=Decimal("100"), qty=Decimal("0.1")),
            BookLevel(price=Decimal("99"), qty=Decimal("10")),
            BookLevel(price=Decimal("98"), qty=Decimal("0.1")),
        )
        score = compute_wall_score_x1000(levels)
        assert score == 100000

    def test_wall_bid_scenario(self) -> None:
        """wall_bid fixture: quantities 0.120, 2.500, 0.140, 0.160, 0.180."""
        # sorted: 0.120, 0.140, 0.160, 0.180, 2.500
        # median (5 elements) = quantities[2] = 0.160
        # max = 2.500, ratio = 2.5 / 0.16 = 15.625 -> 15625 x1000
        levels = (
            BookLevel(price=Decimal("70810.90"), qty=Decimal("0.120")),
            BookLevel(price=Decimal("70810.50"), qty=Decimal("2.500")),
            BookLevel(price=Decimal("70810.00"), qty=Decimal("0.140")),
            BookLevel(price=Decimal("70809.50"), qty=Decimal("0.160")),
            BookLevel(price=Decimal("70809.00"), qty=Decimal("0.180")),
        )
        score = compute_wall_score_x1000(levels)
        assert score == 15625


class TestL2FeatureSnapshot:
    """Tests for L2FeatureSnapshot."""

    def test_from_dict_roundtrip(self) -> None:
        """to_dict/from_dict preserves all fields."""
        snapshot = L2FeatureSnapshot(
            ts_ms=1000,
            symbol="BTCUSDT",
            venue="binance",
            depth=5,
            depth_bid_qty=Decimal("10"),
            depth_ask_qty=Decimal("8"),
            depth_imbalance_bps=1111,
            impact_buy_bps=5,
            impact_sell_bps=3,
            impact_buy_insufficient_depth=0,
            impact_sell_insufficient_depth=0,
            wall_bid_score_x1000=1500,
            wall_ask_score_x1000=1200,
            qty_ref=Decimal("0.003"),
        )
        d = snapshot.to_dict()
        restored = L2FeatureSnapshot.from_dict(d)
        assert restored == snapshot


class TestFixtureScenarios:
    """Tests for 4 canonical fixture scenarios per SPEC B.5."""

    @pytest.fixture
    def snapshots(self) -> dict[str, L2Snapshot]:
        """Load and index all fixture snapshots by scenario."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"
        all_snapshots = load_l2_fixtures(str(path))
        return {s.meta.get("scenario"): s for s in all_snapshots}

    def test_normal_impact_zero(self, snapshots: dict[str, L2Snapshot]) -> None:
        """normal scenario: impact = 0 at qty_ref = 0.003.

        Per SPEC B.5.1: Entire qty fits in top ask level (0.110 > 0.003).
        """
        snap = snapshots["normal"]
        features = L2FeatureSnapshot.from_l2_snapshot(snap)

        assert features.impact_buy_bps == 0
        assert features.impact_sell_bps == 0
        assert features.impact_buy_insufficient_depth == 0
        assert features.impact_sell_insufficient_depth == 0

    def test_ultra_thin_impact_2(self, snapshots: dict[str, L2Snapshot]) -> None:
        """ultra_thin scenario: impact = 2 at qty_ref = 0.003.

        Per SPEC B.5.2:
        Buy: fills 0.001 @ 70830.00, 0.002 @ 70851.25; VWAP ≈ 70844.17, slippage ≈ 2 bps
        Sell: fills 0.001 @ 70829.50, 0.002 @ 70808.25; VWAP ≈ 70815.33, slippage ≈ 2 bps
        """
        snap = snapshots["ultra_thin"]
        features = L2FeatureSnapshot.from_l2_snapshot(snap)

        assert features.impact_buy_bps == 2
        assert features.impact_sell_bps == 2
        assert features.impact_buy_insufficient_depth == 0
        assert features.impact_sell_insufficient_depth == 0

    def test_wall_bid_score_15625(self, snapshots: dict[str, L2Snapshot]) -> None:
        """wall_bid scenario: wall_bid_score = 15625.

        Per SPEC B.5.3:
        - max=2.500, median=0.160, ratio=15.625 -> 15625 x1000
        - wall_ask_score: max=0.190, median=0.150, ratio≈1.267 -> 1267 x1000
        """
        snap = snapshots["wall_bid"]
        features = L2FeatureSnapshot.from_l2_snapshot(snap)

        assert features.wall_bid_score_x1000 == 15625
        assert features.wall_ask_score_x1000 == 1267

        # Impact should be 0 at default qty_ref
        assert features.impact_buy_bps == 0
        assert features.impact_sell_bps == 0

    def test_thin_insufficient_at_qty_0_1(self, snapshots: dict[str, L2Snapshot]) -> None:
        """thin_insufficient scenario: insufficient at qty_ref = 0.1.

        Per SPEC B.5.4:
        - At qty_ref=0.003: impact = 0 (0.003 < 0.009 top ask)
        - At qty_ref=0.1: INSUFFICIENT (total depth < 0.1)
        """
        snap = snapshots["thin_insufficient"]

        # At default qty_ref = 0.003
        features_default = L2FeatureSnapshot.from_l2_snapshot(snap)
        assert features_default.impact_buy_bps == 0
        assert features_default.impact_sell_bps == 0
        assert features_default.impact_buy_insufficient_depth == 0
        assert features_default.impact_sell_insufficient_depth == 0

        # At qty_ref = 0.1
        features_large = L2FeatureSnapshot.from_l2_snapshot(snap, Decimal("0.1"))
        assert features_large.impact_buy_bps == IMPACT_INSUFFICIENT_DEPTH_BPS
        assert features_large.impact_sell_bps == IMPACT_INSUFFICIENT_DEPTH_BPS
        assert features_large.impact_buy_insufficient_depth == 1
        assert features_large.impact_sell_insufficient_depth == 1


class TestDeterminism:
    """Determinism tests for L2 feature computation."""

    def test_feature_computation_determinism(self) -> None:
        """Same L2Snapshot produces identical features across 2 runs."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"

        def compute_digest() -> str:
            snapshots = load_l2_fixtures(str(path))
            features = [L2FeatureSnapshot.from_l2_snapshot(s).to_dict() for s in snapshots]
            combined = str(features)
            return hashlib.sha256(combined.encode()).hexdigest()

        digest1 = compute_digest()
        digest2 = compute_digest()

        assert digest1 == digest2, "L2 feature computation not deterministic"

    def test_roundtrip_preserves_features(self) -> None:
        """to_dict/from_dict produces identical features."""
        path = FIXTURES_DIR / "l2_scenarios.jsonl"
        snapshots = load_l2_fixtures(str(path))

        for snap in snapshots:
            original = L2FeatureSnapshot.from_l2_snapshot(snap)
            restored = L2FeatureSnapshot.from_dict(original.to_dict())
            assert restored == original


class TestConstants:
    """Tests for L2 feature constants."""

    def test_qty_ref_baseline(self) -> None:
        """QTY_REF_BASELINE = 0.003 per SPEC B.2."""
        assert Decimal("0.003") == QTY_REF_BASELINE

    def test_impact_insufficient_depth_bps(self) -> None:
        """IMPACT_INSUFFICIENT_DEPTH_BPS = 500 per SPEC B.2."""
        assert IMPACT_INSUFFICIENT_DEPTH_BPS == 500
