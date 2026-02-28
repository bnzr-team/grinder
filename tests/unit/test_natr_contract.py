"""NATR(14) volatility feature normative contract tests (TRD-3a).

Lock down the compute_natr_bps interface and FeatureSnapshot.natr_bps field
as regression contracts:
- Encoding: NATR * 10000, rounded to int (integer basis points)
- Return type: int
- Invariants: non-negative, 0 for insufficient data, 0 for zero close
- Warmup boundary: exactly period+1 bars → non-zero
- Determinism: same input → identical output
- FeatureSnapshot integration: natr_bps field matches compute_natr_bps output

These are CONTRACT tests — if they break, the NATR interface changed.
See docs/23_NATR_CONTRACT.md for the normative specification.
See ADR-078 in docs/DECISIONS.md for the design decision.

What these tests do NOT prove:
- ATR algorithm correctness (covered by test_indicators.py::TestATR)
- FeatureEngine bar construction (covered by test_feature_engine.py)
- Policy consumption of natr_bps (covered by test_adaptive_grid.py)
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from grinder.features.bar import MidBar
from grinder.features.indicators import compute_atr, compute_natr_bps
from grinder.features.types import FeatureSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(
    bar_ts: int = 0,
    open_: str = "100",
    high: str = "100",
    low: str = "100",
    close: str = "100",
    tick_count: int = 1,
) -> MidBar:
    """Create a MidBar with string-to-Decimal conversion."""
    return MidBar(
        bar_ts=bar_ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_count=tick_count,
    )


# ---------------------------------------------------------------------------
# Golden fixture: known bars with pre-computed expected output
# ---------------------------------------------------------------------------

# 15 bars (period+1 for ATR(14)): uniform TR=10, close=100
# ATR(14) = 10, NATR = 10/100 = 0.10, natr_bps = int(0.10 * 10000) = 1000
GOLDEN_BARS = [_make_bar(bar_ts=i * 60_000, high="105", low="95", close="100") for i in range(15)]
GOLDEN_EXPECTED_BPS = 1000  # 10% NATR in bps


# ===========================================================================
# A) Encoding contract
# ===========================================================================


class TestEncodingContract:
    """Verify that compute_natr_bps returns int-encoded NATR * 10000 (bps).

    SSOT formula (docs/23_NATR_CONTRACT.md):
        natr_bps = int((ATR / close * 10000).quantize(1))
    """

    def test_golden_fixture(self) -> None:
        """Golden fixture: 15 uniform bars (TR=10, close=100) → 1000 bps."""
        result = compute_natr_bps(GOLDEN_BARS, period=14)
        assert result == GOLDEN_EXPECTED_BPS, (
            f"Golden fixture failed: expected {GOLDEN_EXPECTED_BPS}, got {result}"
        )

    def test_return_type_is_int(self) -> None:
        """compute_natr_bps always returns int, not float or Decimal."""
        result = compute_natr_bps(GOLDEN_BARS, period=14)
        assert type(result) is int

    def test_encoding_matches_formula(self) -> None:
        """Verify encoding: natr_bps == int(ATR / close * 10000)."""
        atr = compute_atr(GOLDEN_BARS, period=14)
        assert atr is not None
        close = GOLDEN_BARS[-1].close
        expected = int((atr / close * Decimal("10000")).quantize(Decimal("1")))
        actual = compute_natr_bps(GOLDEN_BARS, period=14)
        assert actual == expected


# ===========================================================================
# B) Invariants
# ===========================================================================


class TestInvariants:
    """Verify structural invariants that hold for ALL valid inputs.

    INV-1: natr_bps >= 0 (non-negative)
    INV-2: natr_bps == 0 when len(bars) < period + 1 (warmup guard)
    INV-3: natr_bps == 0 when last close == 0 (division guard)
    """

    def test_non_negative(self) -> None:
        """INV-1: natr_bps is always non-negative."""
        result = compute_natr_bps(GOLDEN_BARS, period=14)
        assert result >= 0

    @pytest.mark.parametrize("n_bars", [0, 1, 5, 10, 14])
    def test_insufficient_bars_returns_zero(self, n_bars: int) -> None:
        """INV-2: returns 0 when bars < period + 1 (need 15 for ATR(14))."""
        bars = [
            _make_bar(bar_ts=i * 60_000, high="105", low="95", close="100") for i in range(n_bars)
        ]
        result = compute_natr_bps(bars, period=14)
        assert result == 0, f"Expected 0 for {n_bars} bars, got {result}"

    def test_zero_close_returns_zero(self) -> None:
        """INV-3: returns 0 when last bar close is 0 (avoids division by zero)."""
        bars = [_make_bar(bar_ts=i * 60_000, high="1", low="0", close="0") for i in range(15)]
        result = compute_natr_bps(bars, period=14)
        assert result == 0

    def test_empty_bars_returns_zero(self) -> None:
        """INV-2 (edge): empty bar list returns 0."""
        result = compute_natr_bps([], period=14)
        assert result == 0


# ===========================================================================
# C) Warmup boundary
# ===========================================================================


class TestWarmupBoundary:
    """Verify exact warmup boundary: period+1 bars needed.

    ATR(14) needs 14 true range values, each needing a prev_close → 15 bars.
    """

    def test_exactly_period_bars_returns_zero(self) -> None:
        """14 bars (exactly period) → 0 (not enough for ATR(14))."""
        bars = [_make_bar(bar_ts=i * 60_000, high="105", low="95", close="100") for i in range(14)]
        result = compute_natr_bps(bars, period=14)
        assert result == 0

    def test_exactly_period_plus_one_returns_nonzero(self) -> None:
        """15 bars (period + 1) → non-zero (minimum data for ATR(14))."""
        bars = [_make_bar(bar_ts=i * 60_000, high="105", low="95", close="100") for i in range(15)]
        result = compute_natr_bps(bars, period=14)
        assert result > 0, f"Expected > 0 for exactly 15 bars, got {result}"


# ===========================================================================
# D) Determinism
# ===========================================================================


class TestDeterminism:
    """Verify determinism: same input → identical output every time.

    Required for replay (ADR-001) and debugging.
    """

    def test_100_calls_identical(self) -> None:
        """100 repeated calls with same bars produce identical results."""
        baseline = compute_natr_bps(GOLDEN_BARS, period=14)
        for i in range(100):
            result = compute_natr_bps(GOLDEN_BARS, period=14)
            assert result == baseline, f"Call {i} diverged: {result} != {baseline}"

    def test_determinism_with_varying_bars(self) -> None:
        """Determinism holds for non-uniform bar data."""
        bars = [
            _make_bar(
                bar_ts=i * 60_000,
                high=str(100 + i),
                low=str(90 + i),
                close=str(95 + i),
            )
            for i in range(20)
        ]
        baseline = compute_natr_bps(bars, period=14)
        for _ in range(50):
            assert compute_natr_bps(bars, period=14) == baseline


# ===========================================================================
# E) FeatureSnapshot integration
# ===========================================================================


class TestFeatureSnapshotIntegration:
    """Verify natr_bps field in FeatureSnapshot matches compute_natr_bps.

    The FeatureSnapshot.natr_bps field is the canonical location of this
    feature — it must use the same encoding as compute_natr_bps.
    """

    def test_snapshot_natr_bps_is_int(self) -> None:
        """FeatureSnapshot.natr_bps field type annotation is int."""
        field_map = {f.name: f for f in dataclasses.fields(FeatureSnapshot)}
        assert "natr_bps" in field_map, "natr_bps field missing from FeatureSnapshot"
        # types.py uses `from __future__ import annotations`, so type is string
        assert field_map["natr_bps"].type == "int", (
            f"natr_bps type should be int, got {field_map['natr_bps'].type}"
        )

    def test_snapshot_to_policy_features_includes_natr_bps(self) -> None:
        """to_policy_features() includes natr_bps key."""

        snap = FeatureSnapshot(
            ts=1_000_000,
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            spread_bps=5,
            imbalance_l1_bps=0,
            thin_l1=Decimal("1.0"),
            natr_bps=1000,
            atr=Decimal("50"),
            sum_abs_returns_bps=0,
            net_return_bps=0,
            range_score=0,
            warmup_bars=20,
        )
        features = snap.to_policy_features()
        assert "natr_bps" in features
        assert features["natr_bps"] == 1000

    def test_snapshot_serialization_roundtrip(self) -> None:
        """to_dict() → from_dict() preserves natr_bps exactly."""

        snap = FeatureSnapshot(
            ts=1_000_000,
            symbol="BTCUSDT",
            mid_price=Decimal("50000"),
            spread_bps=5,
            imbalance_l1_bps=0,
            thin_l1=Decimal("1.0"),
            natr_bps=1000,
            atr=Decimal("50"),
            sum_abs_returns_bps=0,
            net_return_bps=0,
            range_score=0,
            warmup_bars=20,
        )
        d = snap.to_dict()
        assert d["natr_bps"] == 1000
        restored = FeatureSnapshot.from_dict(d)
        assert restored.natr_bps == snap.natr_bps
