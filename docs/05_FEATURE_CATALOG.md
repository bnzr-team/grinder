# GRINDER - Feature Catalog

> Complete feature specifications with formulas, tiers, and implementation

---

## Implementation Status

> **SSOT Notice**: This catalog describes both implemented and planned features.
> Check this table before assuming a feature is available.

| Feature | Status | Engine | Notes |
|---------|--------|--------|-------|
| `mid_price` | **Implemented** | FeatureEngine v1 | L1-based mid price |
| `spread_bps` | **Implemented** | FeatureEngine v1 | L1 spread in bps (int) |
| `imbalance_l1_bps` | **Implemented** | FeatureEngine v1 | L1 queue imbalance |
| `thin_l1` | **Implemented** | FeatureEngine v1 | Boolean thin-book flag |
| `atr` | **Implemented** | FeatureEngine v1 | ATR from mid-bars |
| `natr_bps` | **Implemented (contract-locked)** | FeatureEngine v1 | Normalized ATR in bps. SSOT: `docs/23_NATR_CONTRACT.md`, ADR-078 |
| `sum_abs_returns_bps` | **Implemented** | FeatureEngine v1 | Range horizon feature |
| `net_return_bps` | **Implemented** | FeatureEngine v1 | Range horizon feature |
| `range_score` | **Implemented** | FeatureEngine v1 | Chop detection score |
| `impact_buy_topN_bps` | **Partial (M7)** | L2FeatureSnapshot | L2 VWAP slippage (unit tested, no digest fixture) |
| `impact_sell_topN_bps` | **Partial (M7)** | L2FeatureSnapshot | L2 VWAP slippage (unit tested, no digest fixture) |
| `wall_bid_score_topN_x1000` | **Partial (M7)** | L2FeatureSnapshot | L2 wall detection (unit tested, no digest fixture) |
| `wall_ask_score_topN_x1000` | **Partial (M7)** | L2FeatureSnapshot | L2 wall detection (unit tested, no digest fixture) |
| `depth_imbalance_topN_bps` | **Partial (M7)** | L2FeatureSnapshot | L2 depth imbalance (unit tested, no digest fixture) |
| `microprice` | Unscheduled | — | Spec only |
| `microprice_dev_bps` | Unscheduled | — | Spec only |
| `cvd` | Unscheduled | — | Spec only |
| `cvd_change_*` | Unscheduled | — | Spec only |
| `ofi_*` | Unscheduled | — | Spec only |
| `beta_btc` | Unscheduled | — | Spec only |
| `residual_ret` | Unscheduled | — | Spec only |
| `funding_rate` | Unscheduled | — | Spec only |
| `oi_change_*` | Unscheduled | — | Spec only |
| `depth_imb_*` | Unscheduled | — | Spec only |
| `depth_slope_*` | Unscheduled | — | Spec only |
| `liq_*` | Unscheduled | — | Spec only |
| `tox_score` | Unscheduled | — | Spec only |

**Status legend:**
- **Implemented**: Code exists, tests pass, determinism verified via digest-gated fixtures
- **Partial (M7)**: Code exists, unit tests pass, but NO digest-gated fixture (M7 gap)
- **Planned (M7)**: Scheduled for M7 milestone (L2 FeatureEngine v2)
- **Unscheduled**: Spec/wishlist only, no implementation timeline

> **Reference**: See `src/grinder/features/engine.py` for FeatureEngine v1 implementation.
> See `docs/smart_grid/SPEC_V2_0.md` §B for L2 feature formulas (M7).

---

## 5.1 Feature Registry

```python
@dataclass
class FeatureSpec:
    name: str
    inputs: list[str]
    window: str
    update_freq: str
    output_range: tuple[float, float]
    staleness_ms: int
    cold_start: str  # "zero", "nan", "bootstrap"
    tier: str  # "L1", "L2", "REST", "DERIVED"
```

---

## 5.2 Complete Feature Table

| Feature | Tier | Inputs | Window | Update | Range | Staleness |
|---------|------|--------|--------|--------|-------|-----------|
| `mid` | L1 | bookTicker | instant | tick | (0, ∞) | 2000ms |
| `spread_bps` | L1 | bookTicker | instant | tick | [0, 1000] | 2000ms |
| `microprice` | L1 | bookTicker | instant | tick | (0, ∞) | 2000ms |
| `microprice_dev_bps` | L1 | microprice, mid | instant | tick | [-100, 100] | 2000ms |
| `queue_imbalance` | L1 | bookTicker | instant | tick | [-1, 1] | 2000ms |
| `ret_1m` | TRADE | aggTrade | 1m | 1s | [-0.1, 0.1] | 5000ms |
| `ret_5m` | TRADE | aggTrade | 5m | 1s | [-0.2, 0.2] | 5000ms |
| `ret_15m` | TRADE | aggTrade | 15m | 1s | [-0.3, 0.3] | 5000ms |
| `volume_ratio_1m` | TRADE | aggTrade | 1m/60m | 1s | [0, 20] | 5000ms |
| `cvd` | TRADE | aggTrade | cumulative | tick | (-∞, ∞) | 5000ms |
| `cvd_change_1m` | TRADE | cvd | 1m | 1s | (-∞, ∞) | 5000ms |
| `cvd_change_5m` | TRADE | cvd | 5m | 1s | (-∞, ∞) | 5000ms |
| `cvd_price_div` | DERIVED | cvd_change, ret | 5m | 1s | {0, 1} | 5000ms |
| `natr_14_5m` | TRADE | aggTrade | 5m×14 | 5s | [0, 0.1] | 10000ms |
| `beta_btc` | TRADE | aggTrade | 60s | 5s | [-2, 2] | 30000ms |
| `residual_ret` | DERIVED | ret, beta_btc | 5m | 5s | [-0.1, 0.1] | 30000ms |
| `funding_rate` | REST | premiumIndex | 8h | 1m | [-0.01, 0.01] | 3600000ms |
| `oi_change_1h` | REST | openInterest | 1h | 5m | [-0.5, 0.5] | 600000ms |
| `depth_imb_5` | L2 | depth | instant | 250ms | [-1, 1] | 3000ms |
| `depth_imb_10` | L2 | depth | instant | 250ms | [-1, 1] | 3000ms |
| `depth_slope_bid` | L2 | depth | instant | 250ms | [0, 1] | 3000ms |
| `depth_slope_ask` | L2 | depth | instant | 250ms | [0, 1] | 3000ms |
| `ofi_10s` | L2 | depth | 10s | tick | (-∞, ∞) | 3000ms |
| `ofi_zscore` | L2 | ofi_10s | 5m | tick | [-5, 5] | 3000ms |
| `spread_at_1k` | L2 | depth | instant | 250ms | [0, 100] | 3000ms |
| `impact_1k_bps` | L2 | depth | instant | 250ms | [0, 50] | 3000ms |
| `wall_bid_dist` | L2 | depth | instant | 250ms | [0, 500] | 3000ms |
| `wall_ask_dist` | L2 | depth | instant | 250ms | [0, 500] | 3000ms |
| `liq_imbalance` | LIQ | forceOrder | 1m | tick | [-1, 1] | 60000ms |
| `liq_surge` | LIQ | forceOrder | 1m | tick | {0, 1} | 60000ms |
| `trend_slope_5m` | DERIVED | mid | 5m | 1s | (-∞, ∞) | 5000ms |
| `price_jump_bps_1m` | DERIVED | mid | 1m | 1s | [-5000, 5000] | 5000ms |
| `depth_top5_usd` | DERIVED | depth | instant | 250ms | [0, ∞) | 3000ms |
| `wall_persistence_score` | DERIVED | depth | 10 ticks | 250ms | [0, 1] | 3000ms |
| `tox_score` | DERIVED | multiple | instant | 1s | [0, 100] | 5000ms |

---

## 5.3 L1 Features (bookTicker)

### Basic Price Features

```python
def calc_mid(bid: float, ask: float) -> float:
    """Simple midpoint price."""
    return (bid + ask) / 2.0

def calc_spread_bps(bid: float, ask: float) -> float:
    """Bid-ask spread in basis points."""
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 10000

def calc_microprice(bid: float, ask: float,
                    bid_qty: float, ask_qty: float) -> float:
    """Volume-weighted fair price estimate."""
    total_qty = bid_qty + ask_qty
    if total_qty <= 0:
        return (bid + ask) / 2.0
    return (ask * bid_qty + bid * ask_qty) / total_qty

def calc_microprice_dev_bps(microprice: float, mid: float) -> float:
    """Deviation of microprice from mid in bps."""
    return (microprice - mid) / mid * 10000

def calc_queue_imbalance(bid_qty: float, ask_qty: float) -> float:
    """Order queue imbalance at BBO. Range: [-1, +1]."""
    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total
```

---

## 5.4 Trade Features (aggTrade)

### Returns

```python
class ReturnCalculator:
    """Calculate returns over various windows."""

    def __init__(self):
        self.prices: deque[tuple[int, float]] = deque()

    def update(self, ts: int, price: float) -> dict[str, float]:
        """Update with new price and return all return values."""
        self.prices.append((ts, price))

        # Prune old data (keep 1h)
        cutoff = ts - 3600_000
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.popleft()

        returns = {}
        for window_name, window_ms in [("1m", 60_000), ("5m", 300_000),
                                        ("15m", 900_000), ("1h", 3600_000)]:
            target_ts = ts - window_ms
            past_price = self._find_price_at(target_ts)
            if past_price:
                returns[f"ret_{window_name}"] = (price - past_price) / past_price

        return returns
```

### Volume Features

```python
class VolumeCalculator:
    """Calculate volume features."""

    def __init__(self):
        self.volumes: deque[tuple[int, float]] = deque()

    def update(self, ts: int, volume_usd: float) -> dict[str, float]:
        """Update and return volume features."""
        self.volumes.append((ts, volume_usd))

        # Calculate vol_1m
        vol_1m = sum(v for t, v in self.volumes if ts - t <= 60_000)

        # Calculate vol_1h (for ratio)
        vol_1h = sum(v for t, v in self.volumes if ts - t <= 3600_000)

        # Volume ratio
        avg_vol_1m = vol_1h / 60 if vol_1h > 0 else 1
        volume_ratio = vol_1m / avg_vol_1m if avg_vol_1m > 0 else 1

        return {
            "vol_1m": vol_1m,
            "vol_1h": vol_1h,
            "volume_ratio_1m": volume_ratio,
        }
```

### CVD (Cumulative Volume Delta)

```python
class CVDCalculator:
    """Calculate Cumulative Volume Delta."""

    def __init__(self):
        self.cvd: float = 0.0
        self.cvd_history: deque[tuple[int, float]] = deque()

    def update(self, ts: int, buy_vol: float, sell_vol: float) -> dict[str, float]:
        """Update CVD with new trade data."""
        delta = buy_vol - sell_vol
        self.cvd += delta
        self.cvd_history.append((ts, self.cvd))

        # Prune old data
        cutoff = ts - 300_000  # 5 minutes
        while self.cvd_history and self.cvd_history[0][0] < cutoff:
            self.cvd_history.popleft()

        # Calculate changes
        cvd_1m_ago = self._get_cvd_at(ts - 60_000)
        cvd_5m_ago = self._get_cvd_at(ts - 300_000)

        return {
            "cvd": self.cvd,
            "cvd_change_1m": self.cvd - cvd_1m_ago if cvd_1m_ago else 0,
            "cvd_change_5m": self.cvd - cvd_5m_ago if cvd_5m_ago else 0,
        }
```

---

## 5.5 L2 Features (depth stream - Top-K only)

### OFI (Order Flow Imbalance - Cont-Kukanov-Stoikov)

```python
class OFICalculator:
    """Cont-Kukanov-Stoikov Order Flow Imbalance."""

    def __init__(self, window_s: int = 10, zscore_window_s: int = 300):
        self.window_s = window_s
        self.zscore_window_s = zscore_window_s
        self.events: deque[tuple[int, float]] = deque()  # (ts, ofi_event)
        self.prev_bid_price: float = 0
        self.prev_bid_qty: float = 0
        self.prev_ask_price: float = 0
        self.prev_ask_qty: float = 0
        self.ofi_history: deque[float] = deque(maxlen=zscore_window_s)

    def update(self, ts: int, bid_price: float, bid_qty: float,
               ask_price: float, ask_qty: float) -> dict:
        """Update OFI with new book state."""

        # Bid side contribution
        if bid_price > self.prev_bid_price:
            delta_bid = bid_qty
        elif bid_price == self.prev_bid_price:
            delta_bid = bid_qty - self.prev_bid_qty
        else:
            delta_bid = -self.prev_bid_qty

        # Ask side contribution
        if ask_price < self.prev_ask_price:
            delta_ask = ask_qty
        elif ask_price == self.prev_ask_price:
            delta_ask = ask_qty - self.prev_ask_qty
        else:
            delta_ask = -self.prev_ask_qty

        ofi_event = delta_bid - delta_ask

        # Store event
        self.events.append((ts, ofi_event))

        # Prune old events
        cutoff = ts - self.window_s * 1000
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

        # Compute OFI over window
        ofi = sum(e[1] for e in self.events)

        # Update history for z-score
        self.ofi_history.append(ofi)

        # Compute z-score
        if len(self.ofi_history) >= 30:
            mean = np.mean(self.ofi_history)
            std = np.std(self.ofi_history) + 1e-8
            ofi_zscore = np.clip((ofi - mean) / std, -5, 5)
        else:
            ofi_zscore = 0.0

        # Update prev state
        self.prev_bid_price = bid_price
        self.prev_bid_qty = bid_qty
        self.prev_ask_price = ask_price
        self.prev_ask_qty = ask_qty

        return {
            "ofi_10s": ofi,
            "ofi_zscore": ofi_zscore,
        }
```

### Depth Imbalance

```python
def calc_depth_imbalance(bids: list[tuple[float, float]],
                          asks: list[tuple[float, float]],
                          levels: int = 5) -> float:
    """
    Order book depth imbalance.

    Args:
        bids: List of (price, qty) sorted descending by price
        asks: List of (price, qty) sorted ascending by price
        levels: Number of levels to consider

    Returns:
        Imbalance ratio in [-1, +1]. Positive = more bids.
    """
    bid_qty = sum(qty for _, qty in bids[:levels])
    ask_qty = sum(qty for _, qty in asks[:levels])
    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total
```

### Depth Slope

```python
def calc_depth_slope(levels: list[tuple[float, float]], n_levels: int = 5) -> float:
    """
    Depth slope: concentration at top of book.

    High slope = most liquidity at BBO (steep book)
    Low slope = liquidity distributed across levels (flat book)
    """
    if not levels:
        return 0.0

    top_qty = levels[0][1]
    total_qty = sum(qty for _, qty in levels[:n_levels])

    if total_qty <= 0:
        return 0.0

    return top_qty / total_qty
```

### Wall Detection

```python
def detect_walls(depth: DepthData,
                 threshold_mult: float = WALL_SIZE_MULT,
                 max_distance_bps: float = WALL_MAX_DISTANCE_BPS,
                 max_walls: int = 3) -> dict:
    """
    Detect significant liquidity walls in order book.

    Notes:
    - Defaults are SSOT-aligned with `docs/15_CONSTANTS.md` (WALL_* constants).
    - Distance filter is mandatory to avoid counting far-away liquidity.

    Returns:
        wall_bid_nearest_bps: Distance to nearest bid wall
        wall_ask_nearest_bps: Distance to nearest ask wall
        wall_bid_volume: Total bid wall volume
        wall_ask_volume: Total ask wall volume
    """
    mid = (depth.bids[0].price + depth.asks[0].price) / 2

    # Bid side
    avg_bid = np.mean([l.qty for l in depth.bids[:10]])
    bid_walls = [
        l for l in depth.bids
        if (
            l.qty > avg_bid * threshold_mult
            and ((mid - l.price)/ mid * 10000) <= max_distance_bps
        )
    ][:max_walls]

    # Ask side
    avg_ask = np.mean([l.qty for l in depth.asks[:10]])
    ask_walls = [
        l for l in depth.asks
        if (
            l.qty > avg_ask * threshold_mult
            and ((l.price - mid)/ mid * 10000) <= max_distance_bps
        )
    ][:max_walls]

    return {
        "wall_bid_nearest_bps": (mid - bid_walls[0].price) / mid * 10000 if bid_walls else 999,
        "wall_ask_nearest_bps": (ask_walls[0].price - mid) / mid * 10000 if ask_walls else 999,
        "wall_bid_volume": sum(w.qty for w in bid_walls),
        "wall_ask_volume": sum(w.qty for w in ask_walls),
        "wall_bid_count": len(bid_walls),
        "wall_ask_count": len(ask_walls),
    }
```

### Wall Persistence (anti-spoof proxy)

```python
def wall_persistence_score(wall_present_flags: list[bool]) -> float:
    """
    Persistence score in [0,1].

    Inputs:
        wall_present_flags: recent N-ticks boolean series (N=WALL_PERSISTENCE_TICKS)
    Returns:
        fraction of ticks where a valid wall existed.
    """
    if not wall_present_flags:
        return 0.0
    return sum(1 for f in wall_present_flags if f) / len(wall_present_flags)
```

### Regime Inputs (Derived)

```python
def calc_trend_slope_5m(prices: list[tuple[int, float]]) -> float:
    """
    Deterministic slope proxy over last 5m.

    Recommendation: linear regression slope on (t, log(price)).
    Implementation must be deterministic (no randomness).
    """
    ...


def calc_price_jump_bps_1m(mid_now: float, mid_1m_ago: float) -> float:
    """Signed 1m jump in bps."""
    if mid_1m_ago <= 0:
        return 0.0
    return (mid_now - mid_1m_ago) / mid_1m_ago * 10000


def calc_depth_top5_usd(depth: "DepthData") -> float:
    """USD notional liquidity in top-5 levels (both sides)."""
    bid = sum(l.price * l.qty for l in depth.bids[:5])
    ask = sum(l.price * l.qty for l in depth.asks[:5])
    return float(bid + ask)
```

### Price Impact Estimation

```python
def estimate_price_impact(levels: list[tuple[float, float]],
                          order_usd: float,
                          side: str) -> float:
    """
    Estimate price impact for given order size.

    Walks through order book levels to calculate average fill price.
    Returns impact in basis points.
    """
    if not levels:
        return float('nan')

    remaining_usd = order_usd
    total_qty = 0.0
    total_cost = 0.0

    for price, qty in levels:
        level_usd = qty * price
        if level_usd >= remaining_usd:
            # Partial fill at this level
            fill_qty = remaining_usd / price
            total_qty += fill_qty
            total_cost += remaining_usd
            remaining_usd = 0
            break
        else:
            # Full fill at this level
            total_qty += qty
            total_cost += level_usd
            remaining_usd -= level_usd

    if total_qty <= 0:
        return float('nan')

    avg_price = total_cost / total_qty
    best_price = levels[0][0]

    # Impact in bps
    if side == 'buy':
        return (avg_price - best_price) / best_price * 10000
    else:
        return (best_price - avg_price) / best_price * 10000
```

---

## 5.6 Cross-Asset Features

### BTC Beta & Residuals

```python
class BetaCalculator:
    """Calculate rolling beta vs BTC."""

    def __init__(self, window: int = 60):
        self.window = window
        self.alt_returns: deque[float] = deque(maxlen=window)
        self.btc_returns: deque[float] = deque(maxlen=window)

    def update(self, alt_ret: float, btc_ret: float) -> dict[str, float]:
        """Update with new returns."""
        self.alt_returns.append(alt_ret)
        self.btc_returns.append(btc_ret)

        if len(self.alt_returns) < 30:
            return {"beta_btc": 1.0, "residual_ret": 0.0, "r2_btc": 0.0}

        # Calculate beta
        cov = np.cov(self.alt_returns, self.btc_returns)[0, 1]
        var_btc = np.var(self.btc_returns)

        beta = cov / var_btc if var_btc > 0 else 1.0

        # Calculate residual
        residual = alt_ret - beta * btc_ret

        # Calculate R²
        ss_tot = np.var(self.alt_returns) * len(self.alt_returns)
        ss_res = sum(
            (a - beta * b) ** 2
            for a, b in zip(self.alt_returns, self.btc_returns)
        )
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        return {
            "beta_btc": np.clip(beta, -2, 2),
            "residual_ret": residual,
            "r2_btc": r2,
        }
```

---

## 5.7 Derivatives Features

### Funding Rate

```python
def process_funding_rate(raw: dict) -> dict[str, float]:
    """Process funding rate data."""
    funding_rate = float(raw["lastFundingRate"])

    return {
        "funding_rate": funding_rate,
        "funding_rate_ann": funding_rate * 3 * 365,  # Annualized
        "funding_direction": 1 if funding_rate > 0.0001 else (-1 if funding_rate < -0.0001 else 0),
    }
```

### Liquidations

```python
class LiquidationTracker:
    """Track liquidation events."""

    def __init__(self):
        self.events: deque[tuple[int, str, float]] = deque()  # (ts, side, qty)

    def update(self, ts: int, side: str, qty: float) -> dict[str, float]:
        """Update with liquidation event."""
        self.events.append((ts, side, qty))

        # Prune old
        cutoff = ts - 3600_000
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

        # Calculate 1m metrics
        liq_1m = [(s, q) for t, s, q in self.events if ts - t <= 60_000]
        liq_long = sum(q for s, q in liq_1m if s == "SELL")  # Longs liquidated
        liq_short = sum(q for s, q in liq_1m if s == "BUY")  # Shorts liquidated

        total = liq_long + liq_short
        imbalance = (liq_long - liq_short) / total if total > 0 else 0

        # Surge detection
        liq_1h = sum(q for t, s, q in self.events)
        avg_1m = liq_1h / 60 if len(self.events) > 0 else 0
        surge = total > avg_1m * 5 if avg_1m > 0 else False

        return {
            "liq_vol_1m": total,
            "liq_long_1m": liq_long,
            "liq_short_1m": liq_short,
            "liq_imbalance": imbalance,
            "liq_surge": surge,
        }
```

---

## 5.8 Feature Normalization

### Rolling Z-Score (DeepLOB style)

```python
class RollingNormalizer:
    """Rolling z-score normalization with clipping."""

    def __init__(self, window: int = 300, clip_value: float = 5.0,
                 min_samples: int = 30):
        self.window = window
        self.clip_value = clip_value
        self.min_samples = min_samples
        self.values: deque[float] = deque(maxlen=window)

    def update(self, value: float) -> float | None:
        """Add value and return normalized version."""
        self.values.append(value)

        if len(self.values) < self.min_samples:
            return None

        mean = sum(self.values) / len(self.values)
        variance = sum((v - mean) ** 2 for v in self.values) / len(self.values)
        std = variance ** 0.5

        if std < 1e-10:
            return 0.0

        z = (value - mean) / std
        return max(-self.clip_value, min(self.clip_value, z))
```

---

## 5.9 Feature Store

```python
@dataclass
class FeatureSnapshot:
    """Complete feature snapshot for a symbol."""
    ts: int
    symbol: str

    # L1
    mid: float
    spread_bps: float
    microprice_dev_bps: float
    queue_imbalance: float

    # Trade
    ret_1m: float
    ret_5m: float
    vol_1m: float
    volume_ratio: float
    cvd_change_1m: float

    # L2 (optional)
    depth_imb_5: float | None
    ofi_zscore: float | None
    impact_1k_bps: float | None

    # Cross-asset
    beta_btc: float
    residual_ret: float

    # Derivatives
    funding_rate: float
    oi_change_1h: float

    # Computed
    tox_score: float
```
