"""Unit tests for Regime Classifier v1 (ASM-P1-04).

These tests verify the deterministic precedence-based regime classification.
Each test targets a specific precedence level per ADR-021.

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.3, ADR-021
"""

from __future__ import annotations

from decimal import Decimal

from grinder.controller.regime import (
    Regime,
    RegimeConfig,
    RegimeDecision,
    RegimeReason,
    classify_regime,
)
from grinder.features.types import FeatureSnapshot
from grinder.gating.types import GateReason, GatingResult


def make_feature_snapshot(
    *,
    ts: int = 1000,
    symbol: str = "TESTUSDT",
    mid_price: Decimal = Decimal("100"),
    spread_bps: int = 10,
    imbalance_l1_bps: int = 0,
    thin_l1: Decimal = Decimal("1.0"),
    natr_bps: int = 100,
    atr: Decimal | None = Decimal("1.0"),
    sum_abs_returns_bps: int = 500,
    net_return_bps: int = 50,
    range_score: int = 10,
    warmup_bars: int = 20,
) -> FeatureSnapshot:
    """Factory for creating test FeatureSnapshots."""
    return FeatureSnapshot(
        ts=ts,
        symbol=symbol,
        mid_price=mid_price,
        spread_bps=spread_bps,
        imbalance_l1_bps=imbalance_l1_bps,
        thin_l1=thin_l1,
        natr_bps=natr_bps,
        atr=atr,
        sum_abs_returns_bps=sum_abs_returns_bps,
        net_return_bps=net_return_bps,
        range_score=range_score,
        warmup_bars=warmup_bars,
    )


class TestRegimeClassifierPrecedence:
    """Tests for regime classifier precedence order."""

    def test_priority_1_emergency_from_kill_switch(self) -> None:
        """Kill-switch active → EMERGENCY (highest priority)."""
        features = make_feature_snapshot()

        decision = classify_regime(
            features=features,
            kill_switch_active=True,
            toxicity_result=None,
        )

        assert decision.regime == Regime.EMERGENCY
        assert decision.reason == RegimeReason.KILL_SWITCH
        assert decision.confidence == 100
        assert decision.features_used["kill_switch_active"] is True

    def test_priority_1_emergency_overrides_all(self) -> None:
        """Kill-switch takes precedence over toxic, thin book, etc."""
        # Features that would trigger THIN_BOOK or VOL_SHOCK
        features = make_feature_snapshot(
            thin_l1=Decimal("0.01"),  # Would trigger THIN_BOOK
            natr_bps=600,  # Would trigger VOL_SHOCK
        )
        # Toxicity result that would trigger TOXIC
        toxicity = GatingResult.block(GateReason.SPREAD_SPIKE)

        decision = classify_regime(
            features=features,
            kill_switch_active=True,
            toxicity_result=toxicity,
        )

        assert decision.regime == Regime.EMERGENCY
        assert decision.reason == RegimeReason.KILL_SWITCH

    def test_priority_2_toxic_from_spread_spike(self) -> None:
        """Toxicity gate blocked (spread spike) → TOXIC."""
        features = make_feature_snapshot()
        toxicity = GatingResult.block(
            GateReason.SPREAD_SPIKE,
            details={"spread_bps": 60, "threshold": 50},
        )

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=toxicity,
        )

        assert decision.regime == Regime.TOXIC
        assert decision.reason == RegimeReason.SPREAD_SPIKE
        assert decision.confidence == 100
        assert decision.features_used["toxicity_blocked"] is True

    def test_priority_2_toxic_from_price_impact(self) -> None:
        """Toxicity gate blocked (price impact) → TOXIC."""
        features = make_feature_snapshot()
        toxicity = GatingResult.block(
            GateReason.PRICE_IMPACT_HIGH,
            details={"price_impact_bps": 600, "threshold": 500},
        )

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=toxicity,
        )

        assert decision.regime == Regime.TOXIC
        assert decision.reason == RegimeReason.PRICE_IMPACT
        assert decision.confidence == 100

    def test_priority_2_toxic_overrides_thin_book(self) -> None:
        """TOXIC takes precedence over THIN_BOOK."""
        features = make_feature_snapshot(thin_l1=Decimal("0.01"))  # Would trigger THIN_BOOK
        toxicity = GatingResult.block(GateReason.SPREAD_SPIKE)

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=toxicity,
        )

        assert decision.regime == Regime.TOXIC

    def test_priority_3_thin_book_from_thin_l1(self) -> None:
        """Thin L1 depth → THIN_BOOK."""
        config = RegimeConfig(thin_l1_qty=Decimal("0.1"))
        features = make_feature_snapshot(thin_l1=Decimal("0.05"))  # Below threshold

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.THIN_BOOK
        assert decision.reason == RegimeReason.THIN_LIQUIDITY
        assert decision.confidence == 90

    def test_priority_3_thin_book_from_wide_spread(self) -> None:
        """Wide spread → THIN_BOOK."""
        config = RegimeConfig(spread_thin_bps=100)
        features = make_feature_snapshot(spread_bps=150)  # Above threshold

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.THIN_BOOK
        assert decision.reason == RegimeReason.WIDE_SPREAD
        assert decision.confidence == 90

    def test_priority_3_thin_book_overrides_vol_shock(self) -> None:
        """THIN_BOOK takes precedence over VOL_SHOCK."""
        config = RegimeConfig(thin_l1_qty=Decimal("0.1"), vol_shock_natr_bps=500)
        features = make_feature_snapshot(
            thin_l1=Decimal("0.05"),  # Triggers THIN_BOOK
            natr_bps=600,  # Would trigger VOL_SHOCK
        )

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.THIN_BOOK

    def test_priority_4_vol_shock_from_high_natr(self) -> None:
        """High NATR → VOL_SHOCK."""
        config = RegimeConfig(vol_shock_natr_bps=500)
        features = make_feature_snapshot(natr_bps=600)  # Above threshold

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.VOL_SHOCK
        assert decision.reason == RegimeReason.HIGH_VOLATILITY
        assert decision.confidence == 85

    def test_priority_4_vol_shock_overrides_trend(self) -> None:
        """VOL_SHOCK takes precedence over TREND."""
        config = RegimeConfig(
            vol_shock_natr_bps=500,
            trend_net_return_bps=200,
            trend_range_score_max=3,
        )
        features = make_feature_snapshot(
            natr_bps=600,  # Triggers VOL_SHOCK
            net_return_bps=300,  # Would trigger TREND
            range_score=2,  # Trending indicator
        )

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.VOL_SHOCK

    def test_priority_5_trend_up(self) -> None:
        """Positive net return + low range score → TREND_UP."""
        config = RegimeConfig(trend_net_return_bps=200, trend_range_score_max=3)
        features = make_feature_snapshot(
            net_return_bps=300,  # Above threshold, positive
            range_score=2,  # Below max (trending)
        )

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.TREND_UP
        assert decision.reason == RegimeReason.TREND_DETECTED
        assert decision.confidence == 75

    def test_priority_5_trend_down(self) -> None:
        """Negative net return + low range score → TREND_DOWN."""
        config = RegimeConfig(trend_net_return_bps=200, trend_range_score_max=3)
        features = make_feature_snapshot(
            net_return_bps=-300,  # Above threshold (abs), negative
            range_score=2,  # Below max (trending)
        )

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.TREND_DOWN
        assert decision.reason == RegimeReason.TREND_DETECTED

    def test_priority_5_trend_requires_both_conditions(self) -> None:
        """Trend requires BOTH high net return AND low range score."""
        config = RegimeConfig(trend_net_return_bps=200, trend_range_score_max=3)

        # High net return but high range score (choppy) → RANGE
        features_choppy = make_feature_snapshot(
            net_return_bps=300,
            range_score=10,  # High = choppy, not trending
        )
        decision = classify_regime(
            features=features_choppy,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime == Regime.RANGE

        # Low range score but low net return → RANGE
        features_flat = make_feature_snapshot(
            net_return_bps=50,  # Below threshold
            range_score=2,
        )
        decision = classify_regime(
            features=features_flat,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime == Regime.RANGE

    def test_priority_6_range_default(self) -> None:
        """No adverse conditions → RANGE."""
        config = RegimeConfig()
        features = make_feature_snapshot()  # All normal values

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )

        assert decision.regime == Regime.RANGE
        assert decision.reason == RegimeReason.DEFAULT
        assert decision.confidence == 80  # Warmed up


class TestRegimeClassifierBoundary:
    """Tests for boundary conditions and edge cases."""

    def test_no_features_returns_range_warmup(self) -> None:
        """No features (warmup period) → RANGE with WARMUP reason."""
        decision = classify_regime(
            features=None,
            kill_switch_active=False,
            toxicity_result=None,
        )

        assert decision.regime == Regime.RANGE
        assert decision.reason == RegimeReason.WARMUP
        assert decision.confidence == 50  # Lower confidence during warmup

    def test_no_toxicity_result_is_safe(self) -> None:
        """None toxicity result treated as pass."""
        features = make_feature_snapshot()

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=None,  # Not run
        )

        assert decision.regime == Regime.RANGE
        assert decision.reason == RegimeReason.DEFAULT

    def test_toxicity_allowed_does_not_trigger_toxic(self) -> None:
        """Toxicity result ALLOWED does not trigger TOXIC."""
        features = make_feature_snapshot()
        toxicity = GatingResult.allow()

        decision = classify_regime(
            features=features,
            kill_switch_active=False,
            toxicity_result=toxicity,
        )

        assert decision.regime != Regime.TOXIC

    def test_thin_l1_at_boundary(self) -> None:
        """Boundary test: thin_l1 exactly at threshold."""
        config = RegimeConfig(thin_l1_qty=Decimal("0.1"))

        # At threshold → NOT thin book
        features_at = make_feature_snapshot(thin_l1=Decimal("0.1"))
        decision = classify_regime(
            features=features_at,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime != Regime.THIN_BOOK

        # Just below threshold → thin book
        features_below = make_feature_snapshot(thin_l1=Decimal("0.099"))
        decision = classify_regime(
            features=features_below,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime == Regime.THIN_BOOK

    def test_spread_at_boundary(self) -> None:
        """Boundary test: spread_bps at threshold."""
        config = RegimeConfig(spread_thin_bps=100)

        # At threshold → NOT thin book (strict >)
        features_at = make_feature_snapshot(spread_bps=100)
        decision = classify_regime(
            features=features_at,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime != Regime.THIN_BOOK

        # Just above → thin book
        features_above = make_feature_snapshot(spread_bps=101)
        decision = classify_regime(
            features=features_above,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime == Regime.THIN_BOOK

    def test_natr_at_boundary(self) -> None:
        """Boundary test: natr_bps at threshold."""
        config = RegimeConfig(vol_shock_natr_bps=500)

        # At threshold → NOT vol shock (strict >)
        features_at = make_feature_snapshot(natr_bps=500)
        decision = classify_regime(
            features=features_at,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime != Regime.VOL_SHOCK

        # Just above → vol shock
        features_above = make_feature_snapshot(natr_bps=501)
        decision = classify_regime(
            features=features_above,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime == Regime.VOL_SHOCK

    def test_trend_at_boundary(self) -> None:
        """Boundary test: net_return_bps and range_score at thresholds."""
        config = RegimeConfig(trend_net_return_bps=200, trend_range_score_max=3)

        # At net_return threshold (abs(200) not > 200) → NOT trend
        features_at = make_feature_snapshot(net_return_bps=200, range_score=2)
        decision = classify_regime(
            features=features_at,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime != Regime.TREND_UP

        # At range_score max (3 <= 3) → trend (if net return is high)
        features_range_at = make_feature_snapshot(net_return_bps=250, range_score=3)
        decision = classify_regime(
            features=features_range_at,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime == Regime.TREND_UP

        # Above range_score max (4 > 3) → NOT trend
        features_range_above = make_feature_snapshot(net_return_bps=250, range_score=4)
        decision = classify_regime(
            features=features_range_above,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision.regime != Regime.TREND_UP

    def test_warmup_affects_confidence(self) -> None:
        """Confidence is lower when not warmed up."""
        config = RegimeConfig()

        # Warmed up (>= 15 bars) → higher confidence
        features_warm = make_feature_snapshot(warmup_bars=20)
        decision_warm = classify_regime(
            features=features_warm,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision_warm.confidence == 80

        # Not warmed up (< 15 bars) → lower confidence
        features_cold = make_feature_snapshot(warmup_bars=10)
        decision_cold = classify_regime(
            features=features_cold,
            kill_switch_active=False,
            toxicity_result=GatingResult.allow(),
            config=config,
        )
        assert decision_cold.confidence == 60


class TestRegimeDecisionSerialization:
    """Tests for RegimeDecision serialization."""

    def test_to_dict_roundtrip(self) -> None:
        """RegimeDecision survives to_dict/from_dict roundtrip."""
        original = RegimeDecision(
            regime=Regime.VOL_SHOCK,
            reason=RegimeReason.HIGH_VOLATILITY,
            confidence=85,
            features_used={"natr_bps": 600, "threshold": 500},
        )

        as_dict = original.to_dict()
        restored = RegimeDecision.from_dict(as_dict)

        assert restored.regime == original.regime
        assert restored.reason == original.reason
        assert restored.confidence == original.confidence
        assert restored.features_used == original.features_used

    def test_to_dict_format(self) -> None:
        """to_dict produces expected JSON-serializable format."""
        decision = RegimeDecision(
            regime=Regime.EMERGENCY,
            reason=RegimeReason.KILL_SWITCH,
            confidence=100,
            features_used={"kill_switch_active": True},
        )

        d = decision.to_dict()

        assert d["regime"] == "EMERGENCY"
        assert d["reason"] == "KILL_SWITCH"
        assert d["confidence"] == 100
        assert d["features_used"]["kill_switch_active"] is True


class TestRegimeConfigDefaults:
    """Tests for RegimeConfig default values."""

    def test_default_config_values(self) -> None:
        """Verify default config values match spec."""
        config = RegimeConfig()

        assert config.thin_l1_qty == Decimal("0.1")
        assert config.spread_thin_bps == 100
        assert config.vol_shock_natr_bps == 500
        assert config.trend_net_return_bps == 200
        assert config.trend_range_score_max == 3

    def test_config_to_dict(self) -> None:
        """Config serializes correctly."""
        config = RegimeConfig(
            thin_l1_qty=Decimal("0.5"),
            spread_thin_bps=150,
        )

        d = config.to_dict()

        assert d["thin_l1_qty"] == "0.5"
        assert d["spread_thin_bps"] == 150


class TestRegimeEnumValues:
    """Tests for Regime enum stability."""

    def test_all_regimes_have_values(self) -> None:
        """All regime values match expected strings."""
        expected = {
            Regime.RANGE: "RANGE",
            Regime.TREND_UP: "TREND_UP",
            Regime.TREND_DOWN: "TREND_DOWN",
            Regime.VOL_SHOCK: "VOL_SHOCK",
            Regime.THIN_BOOK: "THIN_BOOK",
            Regime.TOXIC: "TOXIC",
            Regime.PAUSED: "PAUSED",
            Regime.EMERGENCY: "EMERGENCY",
        }

        for regime, value in expected.items():
            assert regime.value == value

    def test_all_reasons_have_values(self) -> None:
        """All reason values match expected strings."""
        expected = {
            RegimeReason.DEFAULT: "DEFAULT",
            RegimeReason.KILL_SWITCH: "KILL_SWITCH",
            RegimeReason.SPREAD_SPIKE: "SPREAD_SPIKE",
            RegimeReason.PRICE_IMPACT: "PRICE_IMPACT",
            RegimeReason.THIN_LIQUIDITY: "THIN_LIQUIDITY",
            RegimeReason.WIDE_SPREAD: "WIDE_SPREAD",
            RegimeReason.HIGH_VOLATILITY: "HIGH_VOLATILITY",
            RegimeReason.TREND_DETECTED: "TREND_DETECTED",
            RegimeReason.WARMUP: "WARMUP",
        }

        for reason, value in expected.items():
            assert reason.value == value
