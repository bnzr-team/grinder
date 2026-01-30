# GRINDER - Architecture

> System design, modules, and deployment specifications

---

## 3.1 System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              GRINDER SYSTEM                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐ │
│  │   Binance    │   │   Bybit      │   │    OKX       │   │   Future     │ │
│  │   Connector  │   │   Connector  │   │   Connector  │   │   Exchange   │ │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘ │
│         │                  │                  │                  │          │
│         └──────────────────┴──────────────────┴──────────────────┘          │
│                                    │                                         │
│                                    ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                         EVENT BUS (async)                                ││
│  │   Topics: trades, books, liquidations, funding, orders, fills, system   ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│         │              │              │              │              │        │
│         ▼              ▼              ▼              ▼              ▼        │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ │
│  │  Symbol    │ │  Feature   │ │  Prefilter │ │  Policy    │ │  Risk      │ │
│  │  State     │ │  Engine    │ │  (Top-K)   │ │  Engine    │ │  Manager   │ │
│  │  Manager   │ │            │ │            │ │            │ │            │ │
│  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ │
│        │              │              │              │              │        │
│        └──────────────┴──────────────┴──────────────┴──────────────┘        │
│                                    │                                         │
│                                    ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                       EXECUTION ENGINE                                   ││
│  │   Order Manager │ Fill Tracker │ Position Sync │ Latency Monitor        ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                    │                                         │
│                                    ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                       OBSERVABILITY LAYER                                ││
│  │   Metrics (Prometheus) │ Logs (Structured) │ Traces │ Alerts            ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           OFFLINE SYSTEMS                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐         │
│  │  Recorder  │   │  Backtest  │   │  ML Train  │   │  Model     │         │
│  │            │   │  Engine    │   │  Pipeline  │   │  Registry  │         │
│  └────────────┘   └────────────┘   └────────────┘   └────────────┘         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3.2 Module Responsibilities

| Module | Responsibility | SLA |
|--------|----------------|-----|
| Connector | Exchange communication, reconnection | 99.9% uptime |
| Event Bus | Async message routing | < 1ms latency |
| Symbol State | Per-symbol state management | < 5ms update |
| Feature Engine | Feature computation | < 10ms per symbol |
| Prefilter | Top-K selection | < 100ms per cycle |
| Policy Engine | Mode/param decisions | < 10ms per symbol |
| Risk Manager | Limit enforcement | < 5ms per check |
| Execution Engine | Order lifecycle | < 50ms to exchange |

---

## 3.3 High Availability Design

```
┌─────────────────────────────────────────────────────────────┐
│                    ACTIVE-PASSIVE HA                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│   ┌─────────────┐              ┌─────────────┐              │
│   │   PRIMARY   │◄────────────►│  SECONDARY  │              │
│   │   (Active)  │   Heartbeat  │  (Standby)  │              │
│   └──────┬──────┘   + State    └──────┬──────┘              │
│          │          Sync              │                      │
│          ▼                            ▼                      │
│   ┌─────────────┐              ┌─────────────┐              │
│   │  Exchange   │              │  Exchange   │              │
│   │  (Active)   │              │  (Ready)    │              │
│   └─────────────┘              └─────────────┘              │
│                                                              │
│   Failover trigger:                                          │
│   - Primary heartbeat miss > 5s                             │
│   - Primary health check fail × 3                           │
│   - Manual operator command                                  │
│                                                              │
│   Failover procedure:                                        │
│   1. Secondary cancels all Primary orders                   │
│   2. Secondary syncs position from exchange                 │
│   3. Secondary becomes Active                               │
│   4. Alert sent to operators                                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 3.4 Deployment Topologies

| Topology | Use Case | Latency | Cost |
|----------|----------|---------|------|
| Single Node | Development, testing | High | $ |
| Co-located | Production (single exchange) | Low | $$$ |
| Multi-region | Multi-exchange, HA | Medium | $$$$ |

---

## 3.5 Module Interfaces

### Symbol State Manager

```python
@dataclass
class SymbolState:
    """Complete state for a single symbol."""
    symbol: str
    last_update_ts: int

    # Market data
    mid: Decimal
    bid: Decimal
    ask: Decimal
    spread_bps: float

    # Features
    features: dict[str, float]

    # Position
    position_qty: Decimal
    position_notional: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal

    # Grid state
    grid_mode: GridMode
    active_orders: list[Order]
    pending_fills: list[Fill]

    # Risk
    toxicity_score: float
    session_pnl: Decimal
    session_dd: Decimal

class SymbolStateManager:
    """Manages state for all symbols."""

    def __init__(self):
        self.states: dict[str, SymbolState] = {}

    def get(self, symbol: str) -> SymbolState | None:
        return self.states.get(symbol)

    def update_market_data(self, symbol: str, data: MarketData) -> None:
        """Update market data for symbol."""
        ...

    def update_features(self, symbol: str, features: dict) -> None:
        """Update computed features."""
        ...
```

### Event Bus

```python
class EventBus:
    """Async event bus for inter-module communication."""

    def __init__(self):
        self.subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable) -> None:
        """Subscribe to topic."""
        self.subscribers[topic].append(handler)

    async def publish(self, topic: str, event: Any) -> None:
        """Publish event to topic."""
        for handler in self.subscribers[topic]:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Handler error for {topic}: {e}")
```

---

## 3.6 Data Flow

```
Exchange WS → Connector → Event Bus → Feature Engine → Policy Engine
                                           │                │
                                           ▼                ▼
                                    Symbol State      Execution Engine
                                           │                │
                                           └────────────────┘
                                                    │
                                                    ▼
                                              Risk Manager
                                                    │
                                                    ▼
                                               Exchange
```

### Latency Budget

| Stage | Budget | Actual Target |
|-------|--------|---------------|
| WS receive | 5ms | < 2ms |
| Parse + validate | 2ms | < 1ms |
| Feature compute | 10ms | < 5ms |
| Policy decision | 10ms | < 5ms |
| Risk check | 5ms | < 2ms |
| Order placement | 50ms | < 30ms |
| **Total** | **82ms** | **< 45ms** |

---

## 3.7 Configuration Management

```python
@dataclass
class GrinderConfig:
    """Complete system configuration."""

    # Exchange
    exchange: str = "binance"
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    testnet: bool = False

    # Prefilter
    k_symbols: int = 10
    spread_max_bps: float = 15.0
    vol_min_24h_usd: float = 10_000_000

    # Grid
    spacing_base_bps: float = 10.0
    levels_default: int = 5
    size_base_usd: float = 100.0

    # Risk
    dd_max_session_pct: float = 0.03
    dd_max_daily_pct: float = 0.05
    inv_max_symbol_usd: float = 5000.0

    # Toxicity
    tox_low: float = 1.0
    tox_mid: float = 2.0
    tox_high: float = 3.0

    @classmethod
    def from_yaml(cls, path: Path) -> "GrinderConfig":
        """Load from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    @classmethod
    def from_env(cls) -> "GrinderConfig":
        """Load from environment variables."""
        config = cls()
        for field_name in config.__dataclass_fields__:
            env_name = f"GRINDER_{field_name.upper()}"
            if env_name in os.environ:
                # Type conversion based on field type
                ...
        return config
```

---

## 3.8 Error Handling Strategy

```python
class ErrorHandler:
    """Centralized error handling."""

    def __init__(self, config: ErrorConfig):
        self.config = config
        self.error_counts: dict[str, int] = defaultdict(int)
        self.last_errors: dict[str, datetime] = {}

    async def handle(self, error: Exception, context: str) -> ErrorAction:
        """Handle error and determine action."""

        error_type = type(error).__name__

        # Track error rate
        self.error_counts[error_type] += 1
        self.last_errors[error_type] = datetime.now()

        # Determine severity
        if isinstance(error, CriticalError):
            await self.alert(f"CRITICAL: {context}: {error}")
            return ErrorAction.EMERGENCY_STOP

        if isinstance(error, ConnectionError):
            if self.error_counts[error_type] > self.config.max_reconnect_attempts:
                return ErrorAction.PAUSE_TRADING
            return ErrorAction.RETRY_WITH_BACKOFF

        if isinstance(error, OrderError):
            await self.log_order_error(error)
            return ErrorAction.SKIP_ORDER

        # Default: log and continue
        logger.error(f"{context}: {error}")
        return ErrorAction.CONTINUE
```

---

## 3.9 Health Checks

```python
class HealthChecker:
    """System health monitoring."""

    def __init__(self):
        self.checks: list[HealthCheck] = []

    def register(self, check: HealthCheck) -> None:
        self.checks.append(check)

    async def run_all(self) -> HealthStatus:
        """Run all health checks."""
        results = []
        for check in self.checks:
            try:
                result = await asyncio.wait_for(
                    check.run(),
                    timeout=check.timeout_s
                )
                results.append(result)
            except asyncio.TimeoutError:
                results.append(HealthResult(
                    check.name,
                    healthy=False,
                    message="Timeout"
                ))

        all_healthy = all(r.healthy for r in results)
        return HealthStatus(
            healthy=all_healthy,
            results=results,
            timestamp=datetime.now()
        )

# Standard health checks
health_checker = HealthChecker()
health_checker.register(ExchangeConnectionCheck())
health_checker.register(DataFreshnessCheck())
health_checker.register(PositionSyncCheck())
health_checker.register(RiskLimitsCheck())
health_checker.register(DiskSpaceCheck())
```

---

## 3.10 Graceful Shutdown

```python
async def graceful_shutdown(signal_name: str) -> None:
    """Handle graceful shutdown."""
    logger.info(f"Received {signal_name}, initiating graceful shutdown...")

    # 1. Stop accepting new work
    await policy_engine.stop()

    # 2. Cancel pending orders
    logger.info("Cancelling pending orders...")
    await execution_engine.cancel_all_orders()

    # 3. Wait for in-flight operations
    logger.info("Waiting for in-flight operations...")
    await asyncio.sleep(5)

    # 4. Save state
    logger.info("Saving state...")
    await state_manager.save_checkpoint()

    # 5. Close connections
    logger.info("Closing connections...")
    await connector.close()

    # 6. Final metrics flush
    await metrics.flush()

    logger.info("Shutdown complete")

# Register signal handlers
for sig in (signal.SIGTERM, signal.SIGINT):
    asyncio.get_event_loop().add_signal_handler(
        sig,
        lambda s=sig: asyncio.create_task(graceful_shutdown(s.name))
    )
```
