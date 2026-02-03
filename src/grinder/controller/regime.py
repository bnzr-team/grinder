"""Regime classifier v1 for deterministic market regime classification.

This module implements a precedence-based regime classifier that produces
deterministic classifications from FeatureSnapshot data and gating state.

Decision logic (precedence order):
1. kill_switch active → EMERGENCY
2. toxicity gate blocked → TOXIC
3. thin_l1 < threshold OR spread_bps > threshold → THIN_BOOK
4. natr_bps > vol_shock_bps → VOL_SHOCK
5. trend detection (net_return > threshold, range_score < choppy threshold) → TREND_UP/DOWN
6. else → RANGE

All thresholds are configurable via RegimeConfig (no magic numbers).

See: docs/17_ADAPTIVE_SMART_GRID_V1.md §17.3, ADR-021
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grinder.features.types import FeatureSnapshot
    from grinder.gating.types import GatingResult


class Regime(Enum):
    """Market regime classification.

    Precedence (highest to lowest):
    EMERGENCY > TOXIC > THIN_BOOK > VOL_SHOCK > TREND_UP/DOWN > RANGE

    Values are stable and used in metrics/logs.
    """

    RANGE = "RANGE"  # Default: suitable for grid trading
    TREND_UP = "TREND_UP"  # Clear uptrend detected
    TREND_DOWN = "TREND_DOWN"  # Clear downtrend detected
    VOL_SHOCK = "VOL_SHOCK"  # High volatility spike
    THIN_BOOK = "THIN_BOOK"  # Thin liquidity / wide spread
    TOXIC = "TOXIC"  # Adverse market conditions (toxicity gate fired)
    PAUSED = "PAUSED"  # Trading paused (manual or scheduled)
    EMERGENCY = "EMERGENCY"  # Kill-switch active


class RegimeReason(Enum):
    """Reason codes for regime decisions.

    These values are stable and used in metrics/logs.
    """

    DEFAULT = "DEFAULT"  # No adverse conditions detected
    KILL_SWITCH = "KILL_SWITCH"  # Emergency halt active
    SPREAD_SPIKE = "SPREAD_SPIKE"  # Toxicity: spread too wide
    PRICE_IMPACT = "PRICE_IMPACT"  # Toxicity: adverse price movement
    THIN_LIQUIDITY = "THIN_LIQUIDITY"  # L1 depth below threshold
    WIDE_SPREAD = "WIDE_SPREAD"  # Spread exceeds thin book threshold
    HIGH_VOLATILITY = "HIGH_VOLATILITY"  # NATR spike
    TREND_DETECTED = "TREND_DETECTED"  # Range/trend metrics indicate trend
    WARMUP = "WARMUP"  # Insufficient data for regime detection


@dataclass(frozen=True)
class RegimeConfig:
    """Configuration for regime classifier thresholds.

    All thresholds in basis points (integer) for determinism.

    Attributes:
        thin_l1_qty: Minimum L1 depth on thin side (base asset qty)
        spread_thin_bps: Spread threshold for THIN_BOOK (integer bps)
        vol_shock_natr_bps: NATR threshold for VOL_SHOCK (integer bps)
        trend_net_return_bps: Net return threshold for trend detection (integer bps)
        trend_range_score_max: Max range_score for trend (lower = more trending)
    """

    thin_l1_qty: Decimal = field(default_factory=lambda: Decimal("0.1"))
    spread_thin_bps: int = 100  # 1% spread = thin book
    vol_shock_natr_bps: int = 500  # 5% NATR = vol shock
    trend_net_return_bps: int = 200  # 2% net return for trend
    trend_range_score_max: int = 3  # range_score <= 3 = trending (vs choppy)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "thin_l1_qty": str(self.thin_l1_qty),
            "spread_thin_bps": self.spread_thin_bps,
            "vol_shock_natr_bps": self.vol_shock_natr_bps,
            "trend_net_return_bps": self.trend_net_return_bps,
            "trend_range_score_max": self.trend_range_score_max,
        }


@dataclass(frozen=True)
class RegimeDecision:
    """Output of regime classification.

    Attributes:
        regime: Classified market regime
        reason: Primary reason for the classification
        confidence: Confidence score (0-100, integer)
        features_used: Feature values that drove the decision
    """

    regime: Regime
    reason: RegimeReason
    confidence: int  # 0-100, integer for determinism
    features_used: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "regime": self.regime.value,
            "reason": self.reason.value,
            "confidence": self.confidence,
            "features_used": self.features_used,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RegimeDecision:
        """Create from dict."""
        return cls(
            regime=Regime(d["regime"]),
            reason=RegimeReason(d["reason"]),
            confidence=d["confidence"],
            features_used=d["features_used"],
        )


def classify_regime(  # noqa: PLR0911
    features: FeatureSnapshot | None,
    kill_switch_active: bool,
    toxicity_result: GatingResult | None,
    config: RegimeConfig | None = None,
) -> RegimeDecision:
    """Classify market regime from features and gating state.

    Precedence order (first match wins):
    1. kill_switch_active → EMERGENCY
    2. toxicity_result blocked → TOXIC
    3. thin_l1 < threshold OR spread_bps > spread_thin_bps → THIN_BOOK
    4. natr_bps > vol_shock_natr_bps → VOL_SHOCK
    5. net_return_bps > threshold AND range_score <= max → TREND_UP/DOWN
    6. else → RANGE

    Args:
        features: Computed feature snapshot (None during warmup)
        kill_switch_active: Whether kill-switch is triggered
        toxicity_result: Result from toxicity gate (None if not run)
        config: Threshold configuration (uses defaults if None)

    Returns:
        RegimeDecision with classified regime, reason, and confidence
    """
    if config is None:
        config = RegimeConfig()

    # Priority 1: Kill-switch → EMERGENCY
    if kill_switch_active:
        return RegimeDecision(
            regime=Regime.EMERGENCY,
            reason=RegimeReason.KILL_SWITCH,
            confidence=100,
            features_used={"kill_switch_active": True},
        )

    # Priority 2: Toxicity gate blocked → TOXIC
    if toxicity_result is not None and not toxicity_result.allowed:
        reason_str = toxicity_result.reason.value
        if reason_str == "SPREAD_SPIKE":
            toxic_reason = RegimeReason.SPREAD_SPIKE
        elif reason_str == "PRICE_IMPACT_HIGH":
            toxic_reason = RegimeReason.PRICE_IMPACT
        else:
            toxic_reason = RegimeReason.SPREAD_SPIKE  # Default toxicity reason

        return RegimeDecision(
            regime=Regime.TOXIC,
            reason=toxic_reason,
            confidence=100,
            features_used={
                "toxicity_blocked": True,
                "toxicity_reason": reason_str,
                "toxicity_details": toxicity_result.details,
            },
        )

    # No features = warmup period → RANGE with low confidence
    if features is None:
        return RegimeDecision(
            regime=Regime.RANGE,
            reason=RegimeReason.WARMUP,
            confidence=50,
            features_used={"has_features": False},
        )

    # Priority 3: Thin book (thin_l1 or spread)
    if features.thin_l1 < config.thin_l1_qty:
        return RegimeDecision(
            regime=Regime.THIN_BOOK,
            reason=RegimeReason.THIN_LIQUIDITY,
            confidence=90,
            features_used={
                "thin_l1": str(features.thin_l1),
                "threshold": str(config.thin_l1_qty),
            },
        )

    if features.spread_bps > config.spread_thin_bps:
        return RegimeDecision(
            regime=Regime.THIN_BOOK,
            reason=RegimeReason.WIDE_SPREAD,
            confidence=90,
            features_used={
                "spread_bps": features.spread_bps,
                "threshold": config.spread_thin_bps,
            },
        )

    # Priority 4: Volatility shock
    if features.natr_bps > config.vol_shock_natr_bps:
        return RegimeDecision(
            regime=Regime.VOL_SHOCK,
            reason=RegimeReason.HIGH_VOLATILITY,
            confidence=85,
            features_used={
                "natr_bps": features.natr_bps,
                "threshold": config.vol_shock_natr_bps,
            },
        )

    # Priority 5: Trend detection
    # Trend if: significant net return AND low range_score (not choppy)
    if (
        abs(features.net_return_bps) > config.trend_net_return_bps
        and features.range_score <= config.trend_range_score_max
    ):
        # Determine direction from net return sign
        trend_regime = Regime.TREND_UP if features.net_return_bps > 0 else Regime.TREND_DOWN

        return RegimeDecision(
            regime=trend_regime,
            reason=RegimeReason.TREND_DETECTED,
            confidence=75,
            features_used={
                "net_return_bps": features.net_return_bps,
                "range_score": features.range_score,
                "trend_threshold": config.trend_net_return_bps,
                "range_threshold": config.trend_range_score_max,
            },
        )

    # Default: RANGE (good for grid trading)
    confidence = 80 if features.is_warmed_up else 60

    return RegimeDecision(
        regime=Regime.RANGE,
        reason=RegimeReason.DEFAULT,
        confidence=confidence,
        features_used={
            "natr_bps": features.natr_bps,
            "spread_bps": features.spread_bps,
            "thin_l1": str(features.thin_l1),
            "range_score": features.range_score,
            "net_return_bps": features.net_return_bps,
            "is_warmed_up": features.is_warmed_up,
        },
    )
