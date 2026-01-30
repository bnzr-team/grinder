# GRINDER - Glossary

> All terms used throughout GRINDER documentation

---

## Core Concepts

| Term | Definition |
|------|------------|
| **Grid** | Set of limit orders at regular price intervals |
| **Round-Trip (RT)** | Complete cycle: entry fill → exit fill |
| **Mode** | Grid operating mode: BILATERAL, UNI_LONG, UNI_SHORT, PAUSE |
| **Policy** | Rules that determine mode, spacing, sizing, risk |
| **Toxicity** | Measure of adverse selection risk in order flow |
| **Top-K** | Selected symbols with L2 data enabled |

---

## Market Microstructure

| Term | Formula | Description |
|------|---------|-------------|
| **Mid** | `(bid + ask) / 2` | Simple midpoint |
| **Microprice** | `(ask×bid_qty + bid×ask_qty) / (bid_qty + ask_qty)` | Volume-weighted fair price |
| **VAMP** | Multi-level microprice | Volume-adjusted mid price |
| **Spread** | `(ask - bid) / mid × 10000` | Bid-ask spread in bps |
| **OFI** | `Σ(Δbid - Δask)` | Order Flow Imbalance |
| **CVD** | `Cumsum(buy_vol - sell_vol)` | Cumulative Volume Delta |
| **VPIN** | `|buy_vol - sell_vol| / total_vol` | Volume-sync'd informed trading |

---

## Risk Terms

| Term | Definition |
|------|------------|
| **Inventory** | Net position (long positive, short negative) |
| **Drawdown (DD)** | Peak-to-trough decline in equity |
| **Notional** | Position size × price |
| **Beta-adjusted** | Exposure adjusted for BTC correlation |

---

## Reason Codes

### Mode Selection
| Code | Meaning |
|------|---------|
| `MODE_RANGE_LOW_TOX` | Bilateral mode, toxicity low |
| `MODE_LONG_MOMENTUM` | Unidirectional long, momentum confirmed |
| `MODE_SHORT_MOMENTUM` | Unidirectional short, momentum confirmed |
| `MODE_FUNDING_LONG` | Funding harvest, bias long |
| `MODE_FUNDING_SHORT` | Funding harvest, bias short |

### Pause Reasons
| Code | Meaning |
|------|---------|
| `PAUSE_TOX_HIGH` | Paused due to high toxicity |
| `PAUSE_FEED_STALE` | Paused due to stale data |
| `PAUSE_DD_BREACH` | Paused due to drawdown limit |
| `PAUSE_INV_CAP` | Paused due to inventory cap |
| `PAUSE_MANUAL` | Manual operator pause |

### Throttle Reasons
| Code | Meaning |
|------|---------|
| `THROTTLE_TOX_MID` | Throttled (wider spacing, smaller size) |
| `THROTTLE_SPREAD_WIDE` | Throttled, spread too wide |
| `THROTTLE_VOL_HIGH` | Throttled, volatility elevated |

### Skip Reasons
| Code | Meaning |
|------|---------|
| `SKIP_FILL_PROB_LOW` | Level skipped, fill probability too low |
| `SKIP_SIZE_BELOW_MIN` | Level skipped, size below minimum |
| `SKIP_COST_EXCEEDS_PROFIT` | Level skipped, cost > expected profit |

### Action Reasons
| Code | Meaning |
|------|---------|
| `REDUCE_INV_CAP` | Reducing position, inventory cap reached |
| `EMERGENCY_DD_BREACH` | Emergency exit, drawdown limit breached |
| `EMERGENCY_DD_SESSION` | Session drawdown limit breached |
| `EMERGENCY_DD_DAILY` | Daily drawdown limit breached |
| `EMERGENCY_LOSS_STREAK` | Consecutive loss limit hit |
| `EMERGENCY_EXCHANGE_HALT` | Exchange circuit breaker |

---

## Grid Modes

| Mode | Description |
|------|-------------|
| `PAUSE` | No new orders, maintain existing |
| `BILATERAL` | Orders on both sides (range trading) |
| `UNI_LONG` | Buy grid only (trend following up) |
| `UNI_SHORT` | Sell grid only (trend following down) |
| `THROTTLE` | Reduced activity (wider spacing, smaller size) |
| `EMERGENCY` | Aggressive position reduction |
| `FUNDING_HARVEST` | Biased grid to capture funding |
| `LIQ_RECOVERY` | Post-liquidation mean reversion |
| `VOL_ADAPTIVE` | Volatility-adjusted parameters |
| `MEAN_REVERSION` | Fade extreme moves |

---

## Grid Parameters

| Term | Description |
|------|-------------|
| **Center** | Reference price around which grid is built |
| **Spacing** | Distance between grid levels (in bps) |
| **Levels** | Number of orders on each side of center |
| **Size** | Order quantity at each level |
| **Skew** | Inventory-based adjustment to center price |
| **Refresh** | Interval for grid recalculation |

---

## Data Streams

| Stream | Source | Content |
|--------|--------|---------|
| `aggTrade` | WebSocket | Aggregated trades (price, qty, side, time) |
| `bookTicker` | WebSocket | Best bid/ask with quantities |
| `depth@Nms` | WebSocket | Order book depth (N levels) |
| `forceOrder` | WebSocket | Liquidation events |
| `markPrice` | WebSocket | Mark price and funding info |

---

## Time Windows

| Suffix | Duration | Use Case |
|--------|----------|----------|
| `_1s` | 1 second | Ultra-short-term signals |
| `_10s` | 10 seconds | Short-term flow |
| `_1m` | 1 minute | Standard features |
| `_5m` | 5 minutes | Medium-term trends |
| `_15m` | 15 minutes | Regime detection |
| `_1h` | 1 hour | Context/baseline |
| `_24h` | 24 hours | Daily context |
