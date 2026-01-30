# GRINDER - Data Sources

> Exchange connectivity, streams, and data quality specifications

---

## 2.1 Exchange Abstraction Layer

```python
class ExchangeConnector(Protocol):
    """Abstract exchange interface for multi-exchange support."""

    async def subscribe_trades(self, symbols: list[str]) -> AsyncIterator[Trade]: ...
    async def subscribe_book(self, symbols: list[str], depth: int) -> AsyncIterator[Book]: ...
    async def subscribe_liquidations(self, symbols: list[str]) -> AsyncIterator[Liquidation]: ...
    async def get_funding_rate(self, symbol: str) -> FundingRate: ...
    async def get_open_interest(self, symbol: str) -> OpenInterest: ...
    async def place_order(self, order: OrderRequest) -> OrderResponse: ...
    async def cancel_order(self, order_id: str) -> bool: ...
    async def get_position(self, symbol: str) -> Position: ...
```

---

## 2.2 Supported Exchanges (v1.0)

| Exchange | Status | Streams | REST | Notes |
|----------|--------|---------|------|-------|
| Binance Futures | ✅ Primary | aggTrade, bookTicker, depth, forceOrder | All | Full support |
| Bybit | 🔄 Planned | trade, orderbook | All | Similar API |
| OKX | 🔄 Planned | trades, books | All | Similar API |

---

## 2.3 Stream Specifications (Binance)

| Stream | Update Rate | Latency Target | Staleness Threshold |
|--------|-------------|----------------|---------------------|
| `aggTrade` | Event-driven | < 50ms | 5,000ms |
| `bookTicker` | Event-driven | < 50ms | 2,000ms |
| `depth5@250ms` | 250ms | < 100ms | 3,000ms |
| `forceOrder` | Event-driven | < 100ms | 60,000ms |

---

## 2.4 REST Endpoints

| Endpoint | Call Frequency | Cache TTL | Fallback |
|----------|----------------|-----------|----------|
| `/fapi/v1/premiumIndex` | 1/min | 30s | Use last known |
| `/fapi/v1/fundingRate` | 1/8h | 1h | Use last known |
| `/fapi/v1/openInterest` | 1/5min | 2min | Use last known |
| `/fapi/v1/depth` | On-demand | None | Use WS depth |

---

## 2.5 Data Quality Pipeline

```
Raw Stream → Validator → Deduplicator → Normalizer → Feature Engine
                │              │              │
                ▼              ▼              ▼
           Drop invalid   Drop dupes    Unified format
           Log anomaly    Track gaps    Exchange-agnostic
```

### Validation Rules

```python
def validate_trade(trade: RawTrade) -> tuple[bool, str]:
    """Validate incoming trade data."""

    if trade.price <= 0:
        return False, "INVALID_PRICE"

    if trade.quantity <= 0:
        return False, "INVALID_QUANTITY"

    if trade.timestamp > time.time_ms() + 1000:
        return False, "FUTURE_TIMESTAMP"

    if trade.timestamp < time.time_ms() - 60000:
        return False, "STALE_TIMESTAMP"

    return True, "VALID"
```

### Gap Detection

```python
class GapDetector:
    """Detect gaps in data streams."""

    def __init__(self, max_gap_ms: int = 5000):
        self.last_ts: dict[str, int] = {}
        self.max_gap_ms = max_gap_ms

    def check(self, stream: str, ts: int) -> tuple[bool, int]:
        """Check for gap. Returns (has_gap, gap_ms)."""
        if stream not in self.last_ts:
            self.last_ts[stream] = ts
            return False, 0

        gap = ts - self.last_ts[stream]
        self.last_ts[stream] = ts

        if gap > self.max_gap_ms:
            return True, gap
        return False, 0
```

---

## 2.6 Historical Data Requirements

| Data Type | Min History | Source | Priority |
|-----------|-------------|--------|----------|
| aggTrades | 90 days | Binance Public Data | 🔴 Critical |
| bookTicker | 30 days | Live recording | 🔴 Critical |
| depth (L2) | 30 days | Tardis.dev or recording | 🟠 High |
| fundingRate | 180 days | REST historical | 🟠 High |
| openInterest | 90 days | REST historical | 🟡 Medium |
| forceOrder | 30 days | Live recording | 🟡 Medium |

---

## 2.7 Data Quality Rules

### Staleness Thresholds

| Feed | Threshold | Action |
|------|-----------|--------|
| bookTicker | > 2s stale | Cannot place/adjust grid |
| depth | > 3s stale | L2 features invalid → degrade/pause |
| aggTrade | > 5s stale | Degrade momentum/flow features |
| funding | > 5min stale | Use last known value |

### Outlier Filtering

```python
def is_outlier(value: float, history: list[float],
               threshold_sigma: float = 5.0) -> bool:
    """Detect outliers using z-score."""
    if len(history) < 30:
        return False

    mean = np.mean(history)
    std = np.std(history)

    if std < 1e-10:
        return False

    z = abs(value - mean) / std
    return z > threshold_sigma
```

### Gap Handling

| Gap Duration | Action |
|--------------|--------|
| < 1s | Interpolate, continue |
| 1-5s | Mark features as stale, continue |
| 5-30s | Freeze policy, widen grid |
| > 30s | PAUSE mode, wait for recovery |

---

## 2.8 Default Depth Subscription

**Subscription**: `depth5@250ms`

**Rationale**:
- depth5 is sufficient for imbalance/slope and light impact proxies
- 250ms balances latency vs bandwidth
- Enables Top-K (~10) comfortably
- Can scale to ~100 symbols if needed

---

## 2.9 WebSocket Management

```python
class WebSocketManager:
    """Manage WebSocket connections with reconnection."""

    def __init__(self, config: WSConfig):
        self.config = config
        self.connections: dict[str, WebSocket] = {}
        self.reconnect_delay = config.initial_delay_ms

    async def connect(self, url: str, streams: list[str]) -> None:
        """Connect with automatic reconnection."""
        while True:
            try:
                ws = await websockets.connect(url)
                self.connections[url] = ws
                self.reconnect_delay = self.config.initial_delay_ms

                # Subscribe to streams
                await ws.send(json.dumps({
                    "method": "SUBSCRIBE",
                    "params": streams,
                    "id": 1
                }))

                # Process messages
                async for msg in ws:
                    yield json.loads(msg)

            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(self.reconnect_delay / 1000)
                self.reconnect_delay = min(
                    self.reconnect_delay * self.config.backoff_multiplier,
                    self.config.max_delay_ms
                )
```

---

## 2.10 Data Normalization

```python
@dataclass
class NormalizedTrade:
    """Exchange-agnostic trade format."""
    ts: int  # Unix timestamp ms
    symbol: str
    price: Decimal
    quantity: Decimal
    side: Literal["BUY", "SELL"]
    trade_id: str
    is_maker: bool

@dataclass
class NormalizedBook:
    """Exchange-agnostic order book format."""
    ts: int
    symbol: str
    bids: list[tuple[Decimal, Decimal]]  # [(price, qty), ...]
    asks: list[tuple[Decimal, Decimal]]
    sequence: int

def normalize_binance_trade(raw: dict) -> NormalizedTrade:
    """Convert Binance aggTrade to normalized format."""
    return NormalizedTrade(
        ts=raw["T"],
        symbol=raw["s"],
        price=Decimal(raw["p"]),
        quantity=Decimal(raw["q"]),
        side="SELL" if raw["m"] else "BUY",  # m = maker is buyer
        trade_id=str(raw["a"]),
        is_maker=raw["m"],
    )
```
