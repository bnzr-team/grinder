# GRINDER - Grid Policy Library

> Complete specifications for all grid trading policies

---

## 1. Policy Architecture

### 1.1 Policy Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

class MarketRegime(Enum):
    """Market regime inferred by Adaptive Controller."""
    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    VOL_SHOCK = "VOL_SHOCK"
    THIN_BOOK = "THIN_BOOK"
    TOXIC = "TOXIC"
    PAUSED = "PAUSED"

class ResetAction(Enum):
    """Auto-reset action requested by controller."""
    NONE = "NONE"
    SOFT = "SOFT"
    HARD = "HARD"

class GridMode(Enum):
    """Grid operating mode."""
    PAUSE = "PAUSE"
    BILATERAL = "BILATERAL"
    UNI_LONG = "UNI_LONG"
    UNI_SHORT = "UNI_SHORT"
    THROTTLE = "THROTTLE"
    EMERGENCY = "EMERGENCY"

@dataclass(frozen=True)
class GridPlan:
    """Output of grid policy decision."""
    regime: MarketRegime
    mode: GridMode
    center: float
    spacing_bps: float
    width_bps: float
    levels_long: int
    levels_short: int
    size_per_level: float
    skew_bps: float  # Inventory-based center adjustment
    reset_action: ResetAction
    reason_codes: list[str]  # Machine-readable reason codes

@dataclass(frozen=True)
class PolicyContext:
    """Input context for policy decisions."""
    # Market data
    mid: float
    spread_bps: float
    microprice_dev_bps: float

    # Features
    toxicity_score: float
    momentum_1m: float
    momentum_5m: float

    # Regime inputs (derived)
    natr_14_5m: float | None
    trend_slope_5m: float | None
    price_jump_bps_1m: float | None
    depth_top5_usd: float | None

    cvd_change_1m: float
    ofi_zscore: float | None
    depth_imbalance: float | None

    # Position
    inventory: float  # Net position (+ long, - short)
    inventory_pct: float  # As % of max
    unrealized_pnl: float

    # Risk
    session_dd_pct: float
    daily_dd_pct: float

    # Derivatives (optional)
    funding_rate: float | None
    liq_surge: bool

class GridPolicy(Protocol):
    """Protocol for grid policies."""

    @property
    def name(self) -> str:
        """Policy name for logging."""
        ...

    def decide(self, ctx: PolicyContext) -> GridPlan:
        """Generate grid plan from context."""
        ...
```

### 1.1.1 Adaptive Controller integration (SSOT)

- Regime selection, adaptive step, and reset rules are defined in: `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md`
- Policies MUST treat `spacing_bps` as controller-owned (adaptive).
- Policies MUST emit `regime`, `width_bps`, `reset_action`, and `reason_codes` in `GridPlan`.
- **Migration note:** many pseudocode examples below still show the legacy `reason: str` field. Treat it as `reason_codes=[reason]` until the implementation PR migrates the examples.

### 1.2 Policy Selection Logic

```python
def select_policy(ctx: PolicyContext, policies: list[GridPolicy]) -> GridPolicy:
    """
    Select appropriate policy based on context.

    Priority order (highest to lowest):
    1. Emergency (risk limits breached)
    2. Pause (toxicity too high, data stale)
    3. Specific regime policy (funding, liquidation, etc.)
    4. Default policy (range vs trend)
    """
    # Check emergency conditions first
    if ctx.daily_dd_pct >= DD_MAX_DAILY:
        return EmergencyPolicy()

    if ctx.session_dd_pct >= DD_MAX_SESSION:
        return EmergencyPolicy()

    # Check pause conditions
    if ctx.toxicity_score >= TOXICITY_HIGH:
        return PausePolicy(reason="PAUSE_TOX_HIGH")

    # Check for special regimes
    if ctx.funding_rate is not None and abs(ctx.funding_rate) >= FUNDING_EXTREME:
        return FundingHarvesterPolicy()

    if ctx.liq_surge:
        return LiquidationCatcherPolicy()

    # Default: trend or range based on momentum
    if abs(ctx.momentum_5m) >= MOMENTUM_TREND_THRESHOLD:
        return TrendFollowerPolicy()

    return RangeGridPolicy()
```

---

## 2. Core Policies

### 2.1 Range Grid Policy (BILATERAL)

**Purpose**: Profit from mean-reversion in ranging markets.

**Entry Conditions**:
- `toxicity_score < TOXICITY_MID`
- `abs(momentum_5m) < MOMENTUM_TREND_THRESHOLD`
- `spread_bps < SPREAD_MAX_RANGE`

**Grid Configuration**:
- Mode: `BILATERAL`
- Center: `mid + skew` (inventory-adjusted)
- Spacing: Base spacing, tightened when spread is narrow
- Levels: Equal on both sides (reduced if inventory skewed)

```python
class RangeGridPolicy:
    """Bilateral grid for range-bound markets."""

    name = "RANGE_GRID"

    def __init__(
        self,
        base_spacing_bps: float = 10.0,
        base_levels: int = 5,
        base_size: float = 100.0,  # USD per level
        max_skew_bps: float = 20.0,
        inventory_skew_factor: float = 0.5,
    ):
        self.base_spacing_bps = base_spacing_bps
        self.base_levels = base_levels
        self.base_size = base_size
        self.max_skew_bps = max_skew_bps
        self.inventory_skew_factor = inventory_skew_factor

    def decide(self, ctx: PolicyContext) -> GridPlan:
        # Check if conditions are met
        if ctx.toxicity_score >= TOXICITY_MID:
            return self._throttled_plan(ctx, "THROTTLE_TOX_MID")

        if ctx.spread_bps >= SPREAD_MAX_RANGE:
            return self._throttled_plan(ctx, "THROTTLE_SPREAD_WIDE")

        # Calculate inventory skew
        skew_bps = -ctx.inventory_pct * self.inventory_skew_factor * self.max_skew_bps
        skew_bps = max(-self.max_skew_bps, min(self.max_skew_bps, skew_bps))

        # Adjust levels based on inventory
        if ctx.inventory_pct > 0.5:
            # Long heavy: fewer long levels, more short levels
            levels_long = max(1, self.base_levels - 2)
            levels_short = self.base_levels + 1
        elif ctx.inventory_pct < -0.5:
            # Short heavy: more long levels, fewer short levels
            levels_long = self.base_levels + 1
            levels_short = max(1, self.base_levels - 2)
        else:
            levels_long = self.base_levels
            levels_short = self.base_levels

        # Adjust spacing based on spread
        if ctx.spread_bps < 3.0:
            spacing_bps = self.base_spacing_bps * 0.8  # Tighter when spread narrow
        else:
            spacing_bps = self.base_spacing_bps

        return GridPlan(
            mode=GridMode.BILATERAL,
            center=ctx.mid,
            spacing_bps=spacing_bps,
            levels_long=levels_long,
            levels_short=levels_short,
            size_per_level=self.base_size,
            skew_bps=skew_bps,
            reason="MODE_RANGE_LOW_TOX",
        )

    def _throttled_plan(self, ctx: PolicyContext, reason: str) -> GridPlan:
        """Reduced activity plan."""
        return GridPlan(
            mode=GridMode.THROTTLE,
            center=ctx.mid,
            spacing_bps=self.base_spacing_bps * 1.5,
            levels_long=max(1, self.base_levels // 2),
            levels_short=max(1, self.base_levels // 2),
            size_per_level=self.base_size * 0.5,
            skew_bps=0.0,
            reason=reason,
        )
```

### 2.2 Trend Follower Policy (UNI_LONG / UNI_SHORT)

**Purpose**: Capture trending moves with unidirectional grid.

**Entry Conditions**:
- `abs(momentum_5m) >= MOMENTUM_TREND_THRESHOLD`
- `toxicity_score < TOXICITY_HIGH`
- CVD confirming direction

**Grid Configuration**:
- Mode: `UNI_LONG` or `UNI_SHORT` based on trend direction
- Spacing: Wider than range (capture larger moves)
- Levels: Only on trend side

```python
class TrendFollowerPolicy:
    """Unidirectional grid following trend."""

    name = "TREND_FOLLOWER"

    def __init__(
        self,
        trend_spacing_bps: float = 15.0,
        trend_levels: int = 4,
        trend_size: float = 150.0,
        momentum_threshold: float = 2.0,
        exhaustion_threshold: float = 4.0,
    ):
        self.trend_spacing_bps = trend_spacing_bps
        self.trend_levels = trend_levels
        self.trend_size = trend_size
        self.momentum_threshold = momentum_threshold
        self.exhaustion_threshold = exhaustion_threshold

    def decide(self, ctx: PolicyContext) -> GridPlan:
        # Determine trend direction
        is_uptrend = ctx.momentum_5m > self.momentum_threshold
        is_downtrend = ctx.momentum_5m < -self.momentum_threshold

        # Check for exhaustion signals
        if self._is_exhausted(ctx):
            return self._pause_plan("PAUSE_TREND_EXHAUSTED")

        # Check CVD confirmation
        cvd_confirms = self._cvd_confirms_trend(ctx, is_uptrend)

        if not cvd_confirms:
            return self._throttled_plan(ctx, "THROTTLE_CVD_DIVERGENCE")

        # Check if already positioned against trend
        if is_uptrend and ctx.inventory_pct < -0.7:
            # Short heavy in uptrend - don't add more shorts
            return self._pause_plan("PAUSE_WRONG_SIDE")
        if is_downtrend and ctx.inventory_pct > 0.7:
            return self._pause_plan("PAUSE_WRONG_SIDE")

        if is_uptrend:
            return GridPlan(
                mode=GridMode.UNI_LONG,
                center=ctx.mid,
                spacing_bps=self.trend_spacing_bps,
                levels_long=self.trend_levels,
                levels_short=0,
                size_per_level=self.trend_size,
                skew_bps=0.0,
                reason="MODE_LONG_MOMENTUM",
            )
        else:
            return GridPlan(
                mode=GridMode.UNI_SHORT,
                center=ctx.mid,
                spacing_bps=self.trend_spacing_bps,
                levels_long=0,
                levels_short=self.trend_levels,
                size_per_level=self.trend_size,
                skew_bps=0.0,
                reason="MODE_SHORT_MOMENTUM",
            )

    def _is_exhausted(self, ctx: PolicyContext) -> bool:
        """Check for trend exhaustion signals."""
        # Momentum extremely high (potential reversal)
        if abs(ctx.momentum_5m) > self.exhaustion_threshold:
            return True

        # OFI extreme in opposite direction
        if ctx.ofi_zscore is not None:
            if ctx.momentum_5m > 0 and ctx.ofi_zscore < -3:
                return True
            if ctx.momentum_5m < 0 and ctx.ofi_zscore > 3:
                return True

        return False

    def _cvd_confirms_trend(self, ctx: PolicyContext, is_uptrend: bool) -> bool:
        """Check if CVD confirms trend direction."""
        if is_uptrend:
            return ctx.cvd_change_1m > 0
        return ctx.cvd_change_1m < 0

    def _pause_plan(self, reason: str) -> GridPlan:
        return GridPlan(
            mode=GridMode.PAUSE,
            center=0.0,
            spacing_bps=0.0,
            levels_long=0,
            levels_short=0,
            size_per_level=0.0,
            skew_bps=0.0,
            reason=reason,
        )

    def _throttled_plan(self, ctx: PolicyContext, reason: str) -> GridPlan:
        return GridPlan(
            mode=GridMode.THROTTLE,
            center=ctx.mid,
            spacing_bps=self.trend_spacing_bps * 1.5,
            levels_long=2 if ctx.momentum_5m > 0 else 0,
            levels_short=2 if ctx.momentum_5m < 0 else 0,
            size_per_level=self.trend_size * 0.5,
            skew_bps=0.0,
            reason=reason,
        )
```

### 2.3 Funding Harvester Policy

**Purpose**: Capture funding payments when funding rate is extreme.

**Entry Conditions**:
- `abs(funding_rate) >= FUNDING_THRESHOLD`
- `toxicity_score < TOXICITY_HIGH`

**Strategy**:
- Positive funding (longs pay): bias grid short to receive funding
- Negative funding (shorts pay): bias grid long to receive funding

```python
class FundingHarvesterPolicy:
    """Harvest funding payments via biased grid."""

    name = "FUNDING_HARVESTER"

    def __init__(
        self,
        funding_threshold: float = 0.0005,  # 0.05% per 8h
        funding_extreme: float = 0.001,  # 0.1% per 8h
        base_spacing_bps: float = 12.0,
        base_levels: int = 4,
        base_size: float = 120.0,
    ):
        self.funding_threshold = funding_threshold
        self.funding_extreme = funding_extreme
        self.base_spacing_bps = base_spacing_bps
        self.base_levels = base_levels
        self.base_size = base_size

    def decide(self, ctx: PolicyContext) -> GridPlan:
        if ctx.funding_rate is None:
            return self._fallback_plan(ctx)

        funding = ctx.funding_rate

        # Not extreme enough for funding harvest
        if abs(funding) < self.funding_threshold:
            return self._fallback_plan(ctx)

        # Determine bias direction
        # Positive funding = longs pay = we want to be short
        # Negative funding = shorts pay = we want to be long
        bias_short = funding > 0

        # Adjust bias intensity based on funding magnitude
        if abs(funding) >= self.funding_extreme:
            # Strong bias
            if bias_short:
                levels_long = 1
                levels_short = self.base_levels + 2
                reason = "MODE_FUNDING_SHORT_EXTREME"
            else:
                levels_long = self.base_levels + 2
                levels_short = 1
                reason = "MODE_FUNDING_LONG_EXTREME"
        else:
            # Moderate bias
            if bias_short:
                levels_long = 2
                levels_short = self.base_levels
                reason = "MODE_FUNDING_SHORT"
            else:
                levels_long = self.base_levels
                levels_short = 2
                reason = "MODE_FUNDING_LONG"

        # Skew center away from bias direction to increase fills
        skew_bps = 5.0 if bias_short else -5.0

        return GridPlan(
            mode=GridMode.BILATERAL,
            center=ctx.mid,
            spacing_bps=self.base_spacing_bps,
            levels_long=levels_long,
            levels_short=levels_short,
            size_per_level=self.base_size,
            skew_bps=skew_bps,
            reason=reason,
        )

    def _fallback_plan(self, ctx: PolicyContext) -> GridPlan:
        """Fallback to range grid when funding not extreme."""
        return GridPlan(
            mode=GridMode.BILATERAL,
            center=ctx.mid,
            spacing_bps=self.base_spacing_bps,
            levels_long=self.base_levels,
            levels_short=self.base_levels,
            size_per_level=self.base_size,
            skew_bps=0.0,
            reason="MODE_RANGE_FUNDING_NORMAL",
        )
```

### 2.4 Liquidation Catcher Policy

**Purpose**: Capture mean reversion after liquidation cascades.

**Entry Conditions**:
- `liq_surge = True` (liquidation volume spike)
- Cascade appears to be ending (momentum decelerating)

**Strategy**:
- After long liquidations: fade the move, buy into weakness
- After short liquidations: fade the move, sell into strength

```python
class LiquidationCatcherPolicy:
    """Catch mean reversion after liquidation cascades."""

    name = "LIQUIDATION_CATCHER"

    def __init__(
        self,
        recovery_spacing_bps: float = 20.0,  # Wider spacing for volatile conditions
        recovery_levels: int = 3,
        recovery_size: float = 80.0,  # Smaller size due to higher risk
        cooldown_ticks: int = 30,
    ):
        self.recovery_spacing_bps = recovery_spacing_bps
        self.recovery_levels = recovery_levels
        self.recovery_size = recovery_size
        self.cooldown_ticks = cooldown_ticks
        self._surge_start_tick: int | None = None
        self._current_tick: int = 0

    def decide(self, ctx: PolicyContext) -> GridPlan:
        self._current_tick += 1

        if not ctx.liq_surge:
            # No surge active
            self._surge_start_tick = None
            return self._inactive_plan()

        # New surge detected
        if self._surge_start_tick is None:
            self._surge_start_tick = self._current_tick
            return self._pause_plan("PAUSE_LIQ_SURGE_ACTIVE")

        # Wait for cooldown after surge starts
        ticks_since_surge = self._current_tick - self._surge_start_tick
        if ticks_since_surge < self.cooldown_ticks:
            return self._pause_plan("PAUSE_LIQ_COOLDOWN")

        # Determine direction: fade the liquidation direction
        # If momentum is negative (longs liquidated), we buy
        # If momentum is positive (shorts liquidated), we sell
        fade_long = ctx.momentum_1m < 0

        if fade_long:
            return GridPlan(
                mode=GridMode.UNI_LONG,
                center=ctx.mid,
                spacing_bps=self.recovery_spacing_bps,
                levels_long=self.recovery_levels,
                levels_short=0,
                size_per_level=self.recovery_size,
                skew_bps=-10.0,  # Bid lower to catch dip
                reason="MODE_LIQ_RECOVERY_LONG",
            )
        else:
            return GridPlan(
                mode=GridMode.UNI_SHORT,
                center=ctx.mid,
                spacing_bps=self.recovery_spacing_bps,
                levels_long=0,
                levels_short=self.recovery_levels,
                size_per_level=self.recovery_size,
                skew_bps=10.0,  # Offer higher to catch spike
                reason="MODE_LIQ_RECOVERY_SHORT",
            )

    def _inactive_plan(self) -> GridPlan:
        return GridPlan(
            mode=GridMode.PAUSE,
            center=0.0,
            spacing_bps=0.0,
            levels_long=0,
            levels_short=0,
            size_per_level=0.0,
            skew_bps=0.0,
            reason="LIQ_CATCHER_INACTIVE",
        )

    def _pause_plan(self, reason: str) -> GridPlan:
        return GridPlan(
            mode=GridMode.PAUSE,
            center=0.0,
            spacing_bps=0.0,
            levels_long=0,
            levels_short=0,
            size_per_level=0.0,
            skew_bps=0.0,
            reason=reason,
        )
```

### 2.5 Volatility Breakout Policy

**Purpose**: Position for volatility expansion from compression.

**Entry Conditions**:
- Volatility compression (NATR at lows)
- Depth building on one side (anticipation of breakout)

**Strategy**:
- Place grid in breakout direction
- Wider spacing for momentum capture

```python
class VolatilityBreakoutPolicy:
    """Position for volatility expansion."""

    name = "VOL_BREAKOUT"

    def __init__(
        self,
        compression_threshold: float = 0.3,  # NATR z-score
        expansion_threshold: float = 1.5,  # NATR z-score
        breakout_spacing_bps: float = 20.0,
        breakout_levels: int = 3,
        breakout_size: float = 100.0,
    ):
        self.compression_threshold = compression_threshold
        self.expansion_threshold = expansion_threshold
        self.breakout_spacing_bps = breakout_spacing_bps
        self.breakout_levels = breakout_levels
        self.breakout_size = breakout_size

    def decide(self, ctx: PolicyContext) -> GridPlan:
        # Need NATR z-score to determine volatility regime
        # For now, use momentum as proxy for breakout direction

        # Check depth imbalance for direction hint
        if ctx.depth_imbalance is None:
            return self._neutral_plan(ctx)

        # Strong depth imbalance suggests breakout direction
        if ctx.depth_imbalance > 0.4:
            # More bids = anticipating up move
            return GridPlan(
                mode=GridMode.UNI_LONG,
                center=ctx.mid,
                spacing_bps=self.breakout_spacing_bps,
                levels_long=self.breakout_levels,
                levels_short=0,
                size_per_level=self.breakout_size,
                skew_bps=0.0,
                reason="MODE_VOL_BREAKOUT_LONG",
            )
        elif ctx.depth_imbalance < -0.4:
            # More asks = anticipating down move
            return GridPlan(
                mode=GridMode.UNI_SHORT,
                center=ctx.mid,
                spacing_bps=self.breakout_spacing_bps,
                levels_long=0,
                levels_short=self.breakout_levels,
                size_per_level=self.breakout_size,
                skew_bps=0.0,
                reason="MODE_VOL_BREAKOUT_SHORT",
            )

        return self._neutral_plan(ctx)

    def _neutral_plan(self, ctx: PolicyContext) -> GridPlan:
        """Small bilateral grid when direction unclear."""
        return GridPlan(
            mode=GridMode.BILATERAL,
            center=ctx.mid,
            spacing_bps=self.breakout_spacing_bps,
            levels_long=2,
            levels_short=2,
            size_per_level=self.breakout_size * 0.5,
            skew_bps=0.0,
            reason="MODE_VOL_NEUTRAL",
        )
```

### 2.6 Mean Reversion Sniper Policy

**Purpose**: Fade extreme moves that are likely to revert.

**Entry Conditions**:
- Price at extreme (Z-score > 3)
- Volume/momentum divergence (exhaustion signals)
- NOT during liquidation cascade

**Strategy**:
- Counter-trend grid with tight stops

```python
class MeanReversionSniperPolicy:
    """Fade extreme price moves."""

    name = "MEAN_REVERSION_SNIPER"

    def __init__(
        self,
        extreme_threshold: float = 3.0,  # Price z-score
        sniper_spacing_bps: float = 8.0,
        sniper_levels: int = 3,
        sniper_size: float = 60.0,
    ):
        self.extreme_threshold = extreme_threshold
        self.sniper_spacing_bps = sniper_spacing_bps
        self.sniper_levels = sniper_levels
        self.sniper_size = sniper_size

    def decide(self, ctx: PolicyContext) -> GridPlan:
        # Check for exhaustion + extreme
        price_extreme_up = ctx.momentum_5m > self.extreme_threshold
        price_extreme_down = ctx.momentum_5m < -self.extreme_threshold

        if not (price_extreme_up or price_extreme_down):
            return self._inactive_plan()

        # Check for divergence (exhaustion signal)
        has_divergence = self._check_divergence(ctx)

        if not has_divergence:
            return self._inactive_plan()

        # Don't fade during liquidation surge
        if ctx.liq_surge:
            return self._pause_plan("PAUSE_LIQ_SURGE")

        if price_extreme_up:
            # Fade the up move - short
            return GridPlan(
                mode=GridMode.UNI_SHORT,
                center=ctx.mid,
                spacing_bps=self.sniper_spacing_bps,
                levels_long=0,
                levels_short=self.sniper_levels,
                size_per_level=self.sniper_size,
                skew_bps=5.0,  # Offer slightly higher
                reason="MODE_MEAN_REVERSION_SHORT",
            )
        else:
            # Fade the down move - long
            return GridPlan(
                mode=GridMode.UNI_LONG,
                center=ctx.mid,
                spacing_bps=self.sniper_spacing_bps,
                levels_long=self.sniper_levels,
                levels_short=0,
                size_per_level=self.sniper_size,
                skew_bps=-5.0,  # Bid slightly lower
                reason="MODE_MEAN_REVERSION_LONG",
            )

    def _check_divergence(self, ctx: PolicyContext) -> bool:
        """Check for price/flow divergence."""
        # CVD divergence
        if ctx.momentum_5m > 0 and ctx.cvd_change_1m < 0:
            return True
        if ctx.momentum_5m < 0 and ctx.cvd_change_1m > 0:
            return True

        # OFI divergence
        if ctx.ofi_zscore is not None:
            if ctx.momentum_5m > 2 and ctx.ofi_zscore < -1:
                return True
            if ctx.momentum_5m < -2 and ctx.ofi_zscore > 1:
                return True

        return False

    def _inactive_plan(self) -> GridPlan:
        return GridPlan(
            mode=GridMode.PAUSE,
            center=0.0,
            spacing_bps=0.0,
            levels_long=0,
            levels_short=0,
            size_per_level=0.0,
            skew_bps=0.0,
            reason="SNIPER_INACTIVE",
        )

    def _pause_plan(self, reason: str) -> GridPlan:
        return GridPlan(
            mode=GridMode.PAUSE,
            center=0.0,
            spacing_bps=0.0,
            levels_long=0,
            levels_short=0,
            size_per_level=0.0,
            skew_bps=0.0,
            reason=reason,
        )
```

---

## 3. Safety Policies

### 3.1 Pause Policy

```python
class PausePolicy:
    """Pause all grid activity."""

    def __init__(self, reason: str = "PAUSE_MANUAL"):
        self.name = "PAUSE"
        self.reason = reason

    def decide(self, ctx: PolicyContext) -> GridPlan:
        return GridPlan(
            mode=GridMode.PAUSE,
            center=0.0,
            spacing_bps=0.0,
            levels_long=0,
            levels_short=0,
            size_per_level=0.0,
            skew_bps=0.0,
            reason=self.reason,
        )
```

### 3.2 Emergency Policy

```python
class EmergencyPolicy:
    """Aggressive position reduction."""

    name = "EMERGENCY"

    def __init__(
        self,
        exit_spacing_bps: float = 5.0,
        exit_levels: int = 5,
        exit_size_mult: float = 2.0,  # Larger size to exit faster
    ):
        self.exit_spacing_bps = exit_spacing_bps
        self.exit_levels = exit_levels
        self.exit_size_mult = exit_size_mult

    def decide(self, ctx: PolicyContext) -> GridPlan:
        # Determine which side to exit
        if ctx.inventory > 0:
            # Long position - need to sell
            return GridPlan(
                mode=GridMode.EMERGENCY,
                center=ctx.mid,
                spacing_bps=self.exit_spacing_bps,
                levels_long=0,
                levels_short=self.exit_levels,
                size_per_level=abs(ctx.inventory) / self.exit_levels * self.exit_size_mult,
                skew_bps=-10.0,  # Aggressive pricing to exit
                reason="EMERGENCY_EXIT_LONG",
            )
        elif ctx.inventory < 0:
            # Short position - need to buy
            return GridPlan(
                mode=GridMode.EMERGENCY,
                center=ctx.mid,
                spacing_bps=self.exit_spacing_bps,
                levels_long=self.exit_levels,
                levels_short=0,
                size_per_level=abs(ctx.inventory) / self.exit_levels * self.exit_size_mult,
                skew_bps=10.0,  # Aggressive pricing to exit
                reason="EMERGENCY_EXIT_SHORT",
            )
        else:
            # Flat - just pause
            return GridPlan(
                mode=GridMode.PAUSE,
                center=0.0,
                spacing_bps=0.0,
                levels_long=0,
                levels_short=0,
                size_per_level=0.0,
                skew_bps=0.0,
                reason="EMERGENCY_FLAT",
            )
```

---

## 4. Policy Composition

### 4.1 Policy Registry

```python
@dataclass
class PolicyConfig:
    """Configuration for a policy."""
    policy_class: type
    priority: int  # Lower = higher priority
    params: dict


class PolicyRegistry:
    """Registry of available policies with selection logic."""

    def __init__(self):
        self.policies: dict[str, PolicyConfig] = {}
        self._register_defaults()

    def _register_defaults(self):
        self.register("emergency", EmergencyPolicy, priority=0)
        self.register("pause", PausePolicy, priority=10)
        self.register("liq_catcher", LiquidationCatcherPolicy, priority=20)
        self.register("funding", FundingHarvesterPolicy, priority=30)
        self.register("mean_reversion", MeanReversionSniperPolicy, priority=40)
        self.register("trend", TrendFollowerPolicy, priority=50)
        self.register("vol_breakout", VolatilityBreakoutPolicy, priority=60)
        self.register("range", RangeGridPolicy, priority=100)

    def register(
        self,
        name: str,
        policy_class: type,
        priority: int,
        params: dict | None = None,
    ):
        self.policies[name] = PolicyConfig(
            policy_class=policy_class,
            priority=priority,
            params=params or {},
        )

    def get_policy(self, name: str) -> GridPolicy:
        config = self.policies[name]
        return config.policy_class(**config.params)
```

### 4.2 Multi-Policy Orchestrator

```python
class PolicyOrchestrator:
    """Orchestrates multiple policies and selects the best plan."""

    def __init__(self, registry: PolicyRegistry):
        self.registry = registry
        self._active_policy: str | None = None
        self._policy_switch_cooldown: int = 5  # ticks

    def select_plan(self, ctx: PolicyContext) -> tuple[GridPlan, str]:
        """
        Select best plan from registered policies.

        Returns:
            (GridPlan, policy_name)
        """
        # Emergency check first
        if self._check_emergency(ctx):
            policy = self.registry.get_policy("emergency")
            return policy.decide(ctx), "emergency"

        # Evaluate each policy in priority order
        candidates: list[tuple[GridPlan, str, int]] = []

        for name, config in sorted(
            self.registry.policies.items(),
            key=lambda x: x[1].priority
        ):
            if name == "emergency":
                continue

            policy = self.registry.get_policy(name)
            plan = policy.decide(ctx)

            # Only consider active plans (not inactive/pause from non-pause policies)
            if plan.mode != GridMode.PAUSE or name == "pause":
                candidates.append((plan, name, config.priority))

        if not candidates:
            # Fallback to pause
            policy = self.registry.get_policy("pause")
            return policy.decide(ctx), "pause"

        # Select highest priority active plan
        best_plan, best_name, _ = candidates[0]
        return best_plan, best_name

    def _check_emergency(self, ctx: PolicyContext) -> bool:
        """Check if emergency conditions are met."""
        return (
            ctx.daily_dd_pct >= DD_MAX_DAILY or
            ctx.session_dd_pct >= DD_MAX_SESSION
        )
```

---

## 5. Policy Metrics

### 5.1 Per-Policy Tracking

```python
@dataclass
class PolicyMetrics:
    """Metrics for a single policy."""
    name: str
    activations: int = 0
    total_pnl: float = 0.0
    round_trips: int = 0
    avg_rt_bps: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    avg_hold_time_s: float = 0.0


class PolicyTracker:
    """Track policy performance metrics."""

    def __init__(self):
        self.metrics: dict[str, PolicyMetrics] = {}

    def record_activation(self, policy_name: str):
        if policy_name not in self.metrics:
            self.metrics[policy_name] = PolicyMetrics(name=policy_name)
        self.metrics[policy_name].activations += 1

    def record_round_trip(
        self,
        policy_name: str,
        pnl_bps: float,
        hold_time_s: float,
    ):
        if policy_name not in self.metrics:
            self.metrics[policy_name] = PolicyMetrics(name=policy_name)

        m = self.metrics[policy_name]
        m.round_trips += 1
        m.total_pnl += pnl_bps

        # Update running averages
        m.avg_rt_bps = m.total_pnl / m.round_trips
        m.avg_hold_time_s = (
            (m.avg_hold_time_s * (m.round_trips - 1) + hold_time_s) /
            m.round_trips
        )

        # Win rate
        if pnl_bps > 0:
            wins = m.win_rate * (m.round_trips - 1) + 1
        else:
            wins = m.win_rate * (m.round_trips - 1)
        m.win_rate = wins / m.round_trips
```

---

## 6. Policy Configuration (YAML)

```yaml
# grinder/config/policies.yaml

policies:
  range:
    enabled: true
    priority: 100
    params:
      base_spacing_bps: 10.0
      base_levels: 5
      base_size: 100.0
      max_skew_bps: 20.0
      inventory_skew_factor: 0.5

  trend:
    enabled: true
    priority: 50
    params:
      trend_spacing_bps: 15.0
      trend_levels: 4
      trend_size: 150.0
      momentum_threshold: 2.0
      exhaustion_threshold: 4.0

  funding:
    enabled: true
    priority: 30
    params:
      funding_threshold: 0.0005
      funding_extreme: 0.001
      base_spacing_bps: 12.0
      base_levels: 4

  liq_catcher:
    enabled: true
    priority: 20
    params:
      recovery_spacing_bps: 20.0
      recovery_levels: 3
      recovery_size: 80.0
      cooldown_ticks: 30

  mean_reversion:
    enabled: true
    priority: 40
    params:
      extreme_threshold: 3.0
      sniper_spacing_bps: 8.0
      sniper_levels: 3
      sniper_size: 60.0

  vol_breakout:
    enabled: false  # Disabled by default
    priority: 60
    params:
      breakout_spacing_bps: 20.0
      breakout_levels: 3

selection:
  cooldown_ticks: 5
  require_confirmation_ticks: 3
```

---

## 7. Testing Policies

### 7.1 Unit Test Template

```python
import pytest
from grinder.policies import RangeGridPolicy, PolicyContext, GridMode

class TestRangeGridPolicy:
    def setup_method(self):
        self.policy = RangeGridPolicy()

    def test_bilateral_mode_in_normal_conditions(self):
        ctx = PolicyContext(
            mid=50000.0,
            spread_bps=3.0,
            microprice_dev_bps=0.5,
            toxicity_score=20.0,
            momentum_1m=0.5,
            momentum_5m=0.3,

            # Regime inputs (derived)
            natr_14_5m=None,
            trend_slope_5m=None,
            price_jump_bps_1m=None,
            depth_top5_usd=None,

            cvd_change_1m=100.0,
            ofi_zscore=0.5,
            depth_imbalance=0.1,
            inventory=0.0,
            inventory_pct=0.0,
            unrealized_pnl=0.0,
            session_dd_pct=0.0,
            daily_dd_pct=0.0,
            funding_rate=0.0001,
            liq_surge=False,
        )

        plan = self.policy.decide(ctx)

        assert plan.mode == GridMode.BILATERAL
        assert plan.levels_long == 5
        assert plan.levels_short == 5
        assert "RANGE" in plan.reason_codes[0]

    def test_throttle_on_high_toxicity(self):
        ctx = PolicyContext(
            mid=50000.0,
            spread_bps=3.0,
            microprice_dev_bps=0.5,
            toxicity_score=55.0,  # MID toxicity
            momentum_1m=0.5,
            momentum_5m=0.3,

            # Regime inputs (derived)
            natr_14_5m=None,
            trend_slope_5m=None,
            price_jump_bps_1m=None,
            depth_top5_usd=None,

            cvd_change_1m=100.0,
            ofi_zscore=0.5,
            depth_imbalance=0.1,
            inventory=0.0,
            inventory_pct=0.0,
            unrealized_pnl=0.0,
            session_dd_pct=0.0,
            daily_dd_pct=0.0,
            funding_rate=0.0001,
            liq_surge=False,
        )

        plan = self.policy.decide(ctx)

        assert plan.mode == GridMode.THROTTLE
        assert "THROTTLE" in plan.reason_codes[0]

    def test_inventory_skew_adjustment(self):
        ctx = PolicyContext(
            mid=50000.0,
            spread_bps=3.0,
            microprice_dev_bps=0.5,
            toxicity_score=20.0,
            momentum_1m=0.5,
            momentum_5m=0.3,

            # Regime inputs (derived)
            natr_14_5m=None,
            trend_slope_5m=None,
            price_jump_bps_1m=None,
            depth_top5_usd=None,

            cvd_change_1m=100.0,
            ofi_zscore=0.5,
            depth_imbalance=0.1,
            inventory=500.0,
            inventory_pct=0.7,  # 70% of max
            unrealized_pnl=100.0,
            session_dd_pct=0.0,
            daily_dd_pct=0.0,
            funding_rate=0.0001,
            liq_surge=False,
        )

        plan = self.policy.decide(ctx)

        # Should have fewer long levels, more short levels
        assert plan.levels_long < plan.levels_short
        # Should have negative skew (move center down to get more short fills)
        assert plan.skew_bps < 0
```

### 7.2 Integration Test Template

```python
class TestPolicyOrchestrator:
    def test_emergency_overrides_all(self):
        registry = PolicyRegistry()
        orchestrator = PolicyOrchestrator(registry)

        ctx = PolicyContext(
            # ... normal market conditions
            daily_dd_pct=12.0,  # Above DD_MAX_DAILY
        )

        plan, policy_name = orchestrator.select_plan(ctx)

        assert policy_name == "emergency"
        assert plan.mode == GridMode.EMERGENCY

    def test_policy_priority_order(self):
        registry = PolicyRegistry()
        orchestrator = PolicyOrchestrator(registry)

        # Context that would trigger both funding and range
        ctx = PolicyContext(
            # ... conditions that match funding policy
            funding_rate=0.002,  # Extreme funding
        )

        plan, policy_name = orchestrator.select_plan(ctx)

        # Funding has higher priority than range
        assert policy_name == "funding"
```
