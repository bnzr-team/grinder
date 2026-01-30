# GRINDER - Toxicity Specification

> Adverse selection detection and trading response

---

## 6.1 Toxicity Framework

```
┌─────────────────────────────────────────────────────────────┐
│                    TOXICITY DETECTION                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│   Order Flow          Market Impact         Liquidity        │
│   ┌─────────┐         ┌─────────┐         ┌─────────┐       │
│   │  VPIN   │         │  Kyle   │         │ Amihud  │       │
│   │  OFI    │         │ Impact  │         │ Spread  │       │
│   │  CVD    │         │         │         │ Depth   │       │
│   └────┬────┘         └────┬────┘         └────┬────┘       │
│        │                   │                   │             │
│        └───────────────────┴───────────────────┘             │
│                            │                                 │
│                            ▼                                 │
│                   ┌─────────────────┐                       │
│                   │   TOX_SCORE     │                       │
│                   │   (composite)   │                       │
│                   └────────┬────────┘                       │
│                            │                                 │
│            ┌───────────────┼───────────────┐                │
│            ▼               ▼               ▼                │
│      ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│      │ TOX_LOW  │   │ TOX_MID  │   │ TOX_HIGH │            │
│      │  < 1.0   │   │ 1.0-2.0  │   │  > 2.0   │            │
│      │          │   │          │   │          │            │
│      │ Grid OK  │   │ Throttle │   │  Pause   │            │
│      └──────────┘   └──────────┘   └──────────┘            │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 6.2 What is Toxicity?

**Toxicity** measures the probability that incoming order flow contains informed traders who know something the market maker doesn't.

High toxicity means:
- Higher adverse selection risk
- Higher probability of being run over
- Grid fills are more likely to be losing trades

---

## 6.3 Toxicity Components

### 6.3.1 VPIN (Volume-Synchronized Probability of Informed Trading)

```python
def compute_vpin(trades: list[Trade], bucket_size_usd: float,
                 n_buckets: int = 50) -> float:
    """
    VPIN: Probability of informed trading.

    Higher VPIN = more one-sided flow = more likely informed trading.
    """
    buckets = []
    current_bucket_buy = 0.0
    current_bucket_sell = 0.0
    current_bucket_volume = 0.0

    for trade in trades:
        notional = float(trade.price * trade.quantity)

        if trade.side == "BUY":
            current_bucket_buy += notional
        else:
            current_bucket_sell += notional

        current_bucket_volume += notional

        if current_bucket_volume >= bucket_size_usd:
            imbalance = abs(current_bucket_buy - current_bucket_sell)
            buckets.append(imbalance / current_bucket_volume)

            current_bucket_buy = 0.0
            current_bucket_sell = 0.0
            current_bucket_volume = 0.0

    if len(buckets) < n_buckets:
        return 0.0  # Not enough data

    return sum(buckets[-n_buckets:]) / n_buckets
```

### 6.3.2 Kyle's Lambda Proxy

```python
def compute_kyle_proxy(returns: list[float], volumes: list[float]) -> float:
    """
    Kyle's Lambda: Price impact per unit volume.

    Higher lambda = less liquid = more toxic.
    """
    if len(returns) < 10 or len(volumes) < 10:
        return 0.0

    # Sum of |return| / sqrt(volume)
    total = 0.0
    count = 0

    for ret, vol in zip(returns, volumes):
        if vol > 0:
            total += abs(ret) / (vol ** 0.5)
            count += 1

    return total / count if count > 0 else 0.0
```

### 6.3.3 Amihud Illiquidity

```python
def compute_amihud(returns: list[float], volumes_usd: list[float]) -> float:
    """
    Amihud illiquidity measure.

    Higher = more price impact per dollar traded = more toxic.
    """
    if len(returns) < 10:
        return 0.0

    total = 0.0
    count = 0

    for ret, vol in zip(returns, volumes_usd):
        if vol > 1000:  # Min volume threshold
            total += abs(ret) / vol
            count += 1

    return (total / count * 1e6) if count > 0 else 0.0  # Scale for readability
```

### 6.3.4 Spread Widening

```python
def compute_spread_widening(spread_bps: float,
                            spread_avg_1h: float) -> float:
    """
    Spread widening relative to baseline.

    Sudden spread widening indicates market makers pulling liquidity.
    """
    if spread_avg_1h <= 0:
        return 0.0

    return max(0, (spread_bps - spread_avg_1h) / spread_avg_1h)
```

### 6.3.5 OFI Shock

```python
def compute_ofi_shock(ofi_zscore: float) -> float:
    """
    OFI shock component.

    Large OFI (positive or negative) indicates aggressive informed flow.
    """
    return abs(ofi_zscore) / 3.0  # Normalize to ~[0, 1] for z ∈ [-3, 3]
```

### 6.3.6 Liquidation Surge

```python
def detect_liq_surge(liq_volume_1m: float,
                     liq_avg_1h: float,
                     mult: float = 5.0) -> bool:
    """
    Detect liquidation cascade.

    Liquidations cause cascading forced selling/buying.
    """
    if liq_avg_1h <= 0:
        return False

    return liq_volume_1m > liq_avg_1h * mult
```

---

## 6.4 Composite Toxicity Score

```python
def compute_toxicity(features: dict, weights: dict) -> float:
    """
    Composite toxicity score.

    High toxicity = high adverse selection risk = pause/throttle grid.
    """

    components = {
        # VPIN-like: volume imbalance
        "vpin": abs(features.get("cvd_change_1m", 0)) / (features.get("volume_1m", 1) + 1),

        # Kyle proxy: price impact per volume
        "kyle": abs(features.get("ret_1m", 0)) / (features.get("volume_ratio_1m", 1) + 0.1),

        # Amihud: illiquidity
        "amihud": abs(features.get("ret_1m", 0)) / (features.get("volume_1m_usd", 1e6) / 1e6 + 0.01),

        # Spread widening
        "spread_widening": max(0, features.get("spread_bps", 0) - features.get("spread_bps_avg_1h", 5)) / 5,

        # OFI shock
        "ofi_shock": abs(features.get("ofi_zscore", 0)) / 3,

        # Liquidation surge
        "liq_surge": 1.0 if features.get("liq_surge", False) else 0.0,
    }

    # Weighted sum with z-score clipping
    tox_score = sum(
        weights.get(f"W_TOX_{k.upper()}", 0.15) * zscore_clip(v, 0, 3)
        for k, v in components.items()
    )

    return max(0, tox_score)
```

### Component Weights

| Component | Weight | Description |
|-----------|--------|-------------|
| `W_TOX_VPIN` | 0.20 | Volume imbalance |
| `W_TOX_KYLE` | 0.15 | Price impact |
| `W_TOX_AMIHUD` | 0.15 | Illiquidity |
| `W_TOX_SPREAD_WIDENING` | 0.20 | MM pulling liquidity |
| `W_TOX_OFI_SHOCK` | 0.20 | Order flow imbalance |
| `W_TOX_LIQ_SURGE` | 0.10 | Liquidation cascade |

---

## 6.5 Toxicity Actions

| Tox Level | Range | Actions |
|-----------|-------|---------|
| **LOW** | < 1.0 | Full grid operation |
| **MID** | 1.0 - 2.0 | Wider spacing (×1.5), smaller size (×0.6), maker-only |
| **HIGH** | 2.0 - 3.0 | PAUSE new orders, maintain existing |
| **EXTREME** | > 3.0 | EMERGENCY: reduce inventory, cancel all |

### Action Implementation

```python
def apply_toxicity_action(plan: GridPlan, tox_score: float,
                          config: ToxConfig) -> GridPlan:
    """Modify grid plan based on toxicity."""

    if tox_score < config.TOX_LOW:
        # Full operation
        return plan

    if tox_score < config.TOX_MID:
        # Throttle
        return GridPlan(
            mode=GridMode.THROTTLE,
            spacing_bps=plan.spacing_bps * 1.5,
            size_schedule=[s * Decimal("0.6") for s in plan.size_schedule],
            levels_up=max(2, plan.levels_up - 2),
            levels_down=max(2, plan.levels_down - 2),
            maker_only=True,
            reason_codes=plan.reason_codes + ["THROTTLE_TOX_MID"],
            **{k: v for k, v in plan.__dict__.items()
               if k not in ["mode", "spacing_bps", "size_schedule",
                           "levels_up", "levels_down", "maker_only", "reason_codes"]}
        )

    if tox_score < config.TOX_EXTREME:
        # Pause
        return GridPlan(
            mode=GridMode.PAUSE,
            reason_codes=["PAUSE_TOX_HIGH"],
        )

    # Emergency
    return GridPlan(
        mode=GridMode.EMERGENCY,
        reason_codes=["EMERGENCY_TOX_EXTREME"],
    )
```

---

## 6.6 Toxicity Decay

```python
def update_toxicity_with_decay(current_tox: float,
                                new_tox: float,
                                decay_factor: float = 0.95) -> float:
    """
    Exponential decay for toxicity.

    Prevents flip-flopping between modes.
    """
    if new_tox > current_tox:
        # Spike up immediately
        return new_tox
    else:
        # Decay slowly
        return current_tox * decay_factor + new_tox * (1 - decay_factor)
```

### Why Decay?

- **Problem**: Raw toxicity can flip rapidly between HIGH and LOW
- **Effect**: Grid constantly switches between PAUSE and ACTIVE
- **Solution**: Fast spike up, slow decay down
- **Result**: More stable mode transitions

---

## 6.7 Toxicity Cooldown

After HIGH toxicity, require a cooldown period before resuming:

```python
class ToxicityCooldown:
    """Manage cooldown after high toxicity."""

    def __init__(self, cooldown_s: int = 60):
        self.cooldown_s = cooldown_s
        self.last_high_ts: int | None = None

    def update(self, tox_score: float, ts: int) -> bool:
        """Update and return if cooldown is active."""

        if tox_score >= TOX_HIGH:
            self.last_high_ts = ts

        if self.last_high_ts is None:
            return False

        return (ts - self.last_high_ts) < self.cooldown_s * 1000

    def can_resume(self, tox_score: float, ts: int) -> bool:
        """Check if can resume trading."""
        return tox_score < TOX_LOW and not self.update(tox_score, ts)
```

---

## 6.8 Per-Symbol Toxicity

Each symbol maintains independent toxicity tracking:

```python
@dataclass
class SymbolToxicity:
    """Toxicity state for a single symbol."""
    symbol: str
    raw_score: float = 0.0
    smoothed_score: float = 0.0
    regime: str = "LOW"
    last_update_ts: int = 0

    # Components for debugging
    vpin: float = 0.0
    kyle: float = 0.0
    amihud: float = 0.0
    spread_widening: float = 0.0
    ofi_shock: float = 0.0
    liq_surge: bool = False

    # Cooldown
    last_high_ts: int | None = None
    in_cooldown: bool = False
```

---

## 6.9 Toxicity Metrics

| Metric | Description |
|--------|-------------|
| `toxicity_score` | Current smoothed toxicity (gauge) |
| `toxicity_regime` | Current regime label (gauge) |
| `toxicity_transitions` | Mode transitions count (counter) |
| `toxicity_time_in_regime` | Time spent in each regime (histogram) |
| `toxicity_components` | Individual component values (gauge) |

---

## 6.10 Toxicity Alerts

| Alert | Condition | Action |
|-------|-----------|--------|
| `TOXICITY_HIGH` | score > 2.0 | Notify, grid paused |
| `TOXICITY_EXTREME` | score > 3.0 | Page, emergency mode |
| `TOXICITY_SUSTAINED` | HIGH for > 5m | Review symbol |
| `TOXICITY_SPIKE` | score increases > 1.0 in < 10s | Log for analysis |
