"""Tests for prefilter v0 hard gating."""

from __future__ import annotations

import pytest

from grinder.prefilter import (
    OI_MIN_USD,
    SPREAD_MAX_BPS,
    TRADE_COUNT_MIN_1M,
    VOL_MIN_1H_USD,
    VOL_MIN_24H_USD,
    FilterResult,
    hard_filter,
)
from grinder.prefilter.gate import FilterReason


class TestHardFilterAllow:
    """Tests for ALLOW cases."""

    def test_all_gates_pass(self) -> None:
        """Test symbol passes when all features are good."""
        features = {
            "spread_bps": 5.0,  # Well under 15 bps max
            "vol_24h_usd": 50_000_000,  # Well over $10M min
            "vol_1h_usd": 2_000_000,  # Well over $500K min
            "trade_count_1m": 500,  # Well over 100 min
            "oi_usd": 20_000_000,  # Well over $5M min
        }
        result = hard_filter("BTCUSDT", features)

        assert result.allowed is True
        assert result.reason == FilterReason.PASS
        assert result.symbol == "BTCUSDT"

    def test_minimal_features_pass(self) -> None:
        """Test symbol passes with only required features."""
        # Only spread and 24h volume are strictly required
        features = {
            "spread_bps": 10.0,
            "vol_24h_usd": 15_000_000,
        }
        result = hard_filter("ETHUSDT", features)

        assert result.allowed is True
        assert result.reason == FilterReason.PASS

    def test_boundary_values_pass(self) -> None:
        """Test symbol passes at exact threshold boundaries."""
        features = {
            "spread_bps": SPREAD_MAX_BPS,  # Exactly at max (should pass)
            "vol_24h_usd": VOL_MIN_24H_USD,  # Exactly at min (should pass)
            "vol_1h_usd": VOL_MIN_1H_USD,
            "trade_count_1m": TRADE_COUNT_MIN_1M,
            "oi_usd": OI_MIN_USD,
        }
        result = hard_filter("SOLUSDT", features)

        assert result.allowed is True
        assert result.reason == FilterReason.PASS


class TestHardFilterBlock:
    """Tests for BLOCK cases."""

    def test_spread_too_high(self) -> None:
        """Test symbol blocked when spread exceeds max."""
        features = {
            "spread_bps": 20.0,  # Over 15 bps max
            "vol_24h_usd": 50_000_000,
        }
        result = hard_filter("XLMUSDT", features)

        assert result.allowed is False
        assert result.reason == FilterReason.SPREAD_TOO_HIGH
        assert result.symbol == "XLMUSDT"

    def test_vol_24h_too_low(self) -> None:
        """Test symbol blocked when 24h volume too low."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 5_000_000,  # Under $10M min
        }
        result = hard_filter("LOWVOLUSDT", features)

        assert result.allowed is False
        assert result.reason == FilterReason.VOL_24H_TOO_LOW

    def test_vol_1h_too_low(self) -> None:
        """Test symbol blocked when 1h volume too low."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
            "vol_1h_usd": 100_000,  # Under $500K min
        }
        result = hard_filter("LOWVOL1HUSDT", features)

        assert result.allowed is False
        assert result.reason == FilterReason.VOL_1H_TOO_LOW

    def test_activity_too_low(self) -> None:
        """Test symbol blocked when trade count too low."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
            "trade_count_1m": 50,  # Under 100 min
        }
        result = hard_filter("LOWACTUSDT", features)

        assert result.allowed is False
        assert result.reason == FilterReason.ACTIVITY_TOO_LOW

    def test_oi_too_low(self) -> None:
        """Test symbol blocked when open interest too low."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
            "oi_usd": 1_000_000,  # Under $5M min
        }
        result = hard_filter("LOWOIUSDT", features)

        assert result.allowed is False
        assert result.reason == FilterReason.OI_TOO_LOW

    def test_blacklisted(self) -> None:
        """Test symbol blocked when in blacklist."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
        }
        blacklist = frozenset({"SCAMUSDT", "RUGUSDT"})
        result = hard_filter("SCAMUSDT", features, blacklist=blacklist)

        assert result.allowed is False
        assert result.reason == FilterReason.BLACKLISTED

    def test_delisting(self) -> None:
        """Test symbol blocked when marked for delisting."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
            "is_delisting": True,
        }
        result = hard_filter("DEADUSDT", features)

        assert result.allowed is False
        assert result.reason == FilterReason.DELISTING


class TestHardFilterPriority:
    """Tests for filter evaluation order."""

    def test_blacklist_checked_first(self) -> None:
        """Test blacklist is checked before other gates."""
        # Symbol has good metrics but is blacklisted
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
        }
        blacklist = frozenset({"BADUSDT"})
        result = hard_filter("BADUSDT", features, blacklist=blacklist)

        assert result.reason == FilterReason.BLACKLISTED

    def test_delisting_checked_before_metrics(self) -> None:
        """Test delisting is checked before metric gates."""
        features = {
            "spread_bps": 5.0,
            "vol_24h_usd": 50_000_000,
            "is_delisting": True,
        }
        result = hard_filter("DYINGUSDT", features)

        assert result.reason == FilterReason.DELISTING


class TestFilterResult:
    """Tests for FilterResult contract."""

    def test_roundtrip_dict(self) -> None:
        """Test dict serialization roundtrip."""
        result = FilterResult(
            allowed=True,
            reason=FilterReason.PASS,
            symbol="BTCUSDT",
        )
        d = result.to_dict()
        restored = FilterResult.from_dict(d)

        assert restored == result

    def test_roundtrip_dict_blocked(self) -> None:
        """Test dict roundtrip for blocked result."""
        result = FilterResult(
            allowed=False,
            reason=FilterReason.SPREAD_TOO_HIGH,
            symbol="XLMUSDT",
        )
        d = result.to_dict()
        restored = FilterResult.from_dict(d)

        assert restored == result
        assert restored.allowed is False
        assert restored.reason == FilterReason.SPREAD_TOO_HIGH

    def test_frozen(self) -> None:
        """Test FilterResult is immutable."""
        result = FilterResult(
            allowed=True,
            reason=FilterReason.PASS,
            symbol="BTCUSDT",
        )
        with pytest.raises(AttributeError):
            result.allowed = False  # type: ignore[misc]


class TestCustomThresholds:
    """Tests for custom threshold parameters."""

    def test_custom_spread_threshold(self) -> None:
        """Test passing custom spread threshold."""
        features = {"spread_bps": 12.0, "vol_24h_usd": 50_000_000}

        # Default threshold (15 bps) - should pass
        result = hard_filter("TEST", features)
        assert result.allowed is True

        # Stricter threshold (10 bps) - should block
        result = hard_filter("TEST", features, spread_max_bps=10.0)
        assert result.allowed is False
        assert result.reason == FilterReason.SPREAD_TOO_HIGH

    def test_custom_volume_threshold(self) -> None:
        """Test passing custom volume threshold."""
        features = {"spread_bps": 5.0, "vol_24h_usd": 8_000_000}

        # Default threshold ($10M) - should block
        result = hard_filter("TEST", features)
        assert result.allowed is False

        # Relaxed threshold ($5M) - should pass
        result = hard_filter("TEST", features, vol_min_24h_usd=5_000_000)
        assert result.allowed is True
