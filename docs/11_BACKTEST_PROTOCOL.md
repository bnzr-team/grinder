# GRINDER - Backtest Protocol

> Historical testing, validation, and walk-forward optimization

---

## 11.1 Backtest Philosophy

```
┌─────────────────────────────────────────────────────────────────┐
│                    BACKTEST PRINCIPLES                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. REALISTIC COSTS                                             │
│     - Include maker/taker fees                                  │
│     - Model slippage based on order size                        │
│     - Account for funding payments                              │
│                                                                  │
│  2. NO LOOKAHEAD BIAS                                           │
│     - Features computed only from past data                     │
│     - No peeking at future prices for fills                     │
│     - Strict temporal ordering                                  │
│                                                                  │
│  3. REALISTIC FILLS                                             │
│     - Limit orders fill at touch or through                     │
│     - Model partial fills                                       │
│     - Queue position simulation                                 │
│                                                                  │
│  4. WALK-FORWARD VALIDATION                                     │
│     - Train/validation/test splits                              │
│     - Rolling window optimization                               │
│     - Out-of-sample performance                                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 11.2 Data Requirements

### Minimum Historical Data

| Data Type | Min Duration | Granularity | Source |
|-----------|--------------|-------------|--------|
| Trades | 90 days | Tick | Binance Public Data |
| Book (L1) | 30 days | 100ms | Recorded or Tardis |
| Book (L2) | 30 days | 250ms | Recorded or Tardis |
| Funding | 180 days | 8h | REST API |
| OI | 90 days | 5m | REST API |
| Liquidations | 30 days | Event | Recorded |

### Data Format

```python
@dataclass
class BacktestTrade:
    ts: int  # Unix ms
    symbol: str
    price: Decimal
    quantity: Decimal
    side: str  # "BUY" or "SELL"
    is_buyer_maker: bool

@dataclass
class BacktestBook:
    ts: int
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    # Optional L2
    bids: list[tuple[Decimal, Decimal]] | None
    asks: list[tuple[Decimal, Decimal]] | None
```

---

## 11.3 Cost Model

### Fee Structure

```python
@dataclass
class FeeModel:
    """Fee configuration."""
    maker_fee_bps: float = 2.0  # 0.02%
    taker_fee_bps: float = 4.0  # 0.04%

    # Volume-based discounts (optional)
    volume_tiers: list[tuple[float, float, float]] = field(default_factory=list)
    # [(volume_threshold, maker_discount, taker_discount), ...]

def calculate_fee(notional: Decimal, is_maker: bool,
                  fee_model: FeeModel) -> Decimal:
    """Calculate trading fee."""
    rate = fee_model.maker_fee_bps if is_maker else fee_model.taker_fee_bps
    return notional * Decimal(str(rate / 10000))
```

### Slippage Model

```python
def estimate_slippage(order_size_usd: float,
                      book: BacktestBook,
                      side: str) -> float:
    """
    Estimate slippage for order.

    Uses order book depth to calculate realistic fill price.
    """
    if book.bids is None or book.asks is None:
        # L1 only - use simple model
        spread_bps = float((book.ask_price - book.bid_price) /
                          ((book.ask_price + book.bid_price) / 2) * 10000)
        base_slippage = spread_bps * 0.5
        size_impact = order_size_usd / 10000 * 0.5  # 0.5 bps per $10k
        return base_slippage + size_impact

    # L2 available - walk the book
    levels = book.asks if side == "BUY" else book.bids
    return estimate_price_impact(levels, order_size_usd, side)
```

### Funding Model

```python
def calculate_funding_payment(position: Decimal,
                               mark_price: Decimal,
                               funding_rate: Decimal) -> Decimal:
    """
    Calculate funding payment.

    Positive funding: longs pay shorts
    Negative funding: shorts pay longs
    """
    notional = abs(position) * mark_price
    payment = notional * funding_rate

    if position > 0:
        # Long position
        return -payment if funding_rate > 0 else payment
    else:
        # Short position
        return payment if funding_rate > 0 else -payment
```

---

## 11.4 Fill Simulation

### Limit Order Fill Logic

```python
class FillSimulator:
    """Simulate order fills in backtest."""

    def __init__(self, config: FillConfig):
        self.config = config

    def check_fill(self, order: Order,
                   trade: BacktestTrade,
                   book: BacktestBook) -> Fill | None:
        """
        Check if order would fill given market data.

        Rules:
        1. Buy limit fills when trade price <= order price
        2. Sell limit fills when trade price >= order price
        3. Apply queue position logic for maker orders
        """
        if order.side == "BUY":
            if trade.price <= order.price:
                return self._create_fill(order, trade, book)
        else:  # SELL
            if trade.price >= order.price:
                return self._create_fill(order, trade, book)

        return None

    def _create_fill(self, order: Order,
                     trade: BacktestTrade,
                     book: BacktestBook) -> Fill:
        """Create fill with realistic price."""

        # Determine fill price
        if self.config.use_trade_price:
            fill_price = trade.price
        else:
            # Use order price (more conservative for limit orders)
            fill_price = order.price

        # Determine quantity (handle partial fills)
        if self.config.allow_partial_fills:
            fill_qty = min(order.remaining_qty, trade.quantity)
        else:
            fill_qty = order.remaining_qty

        # Determine maker/taker
        is_maker = self._is_maker_fill(order, trade, book)

        return Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=fill_price,
            quantity=fill_qty,
            ts=trade.ts,
            is_maker=is_maker,
        )

    def _is_maker_fill(self, order: Order,
                       trade: BacktestTrade,
                       book: BacktestBook) -> bool:
        """Determine if fill is maker or taker."""
        # POST_ONLY orders are always maker
        if order.time_in_force == "POST_ONLY":
            return True

        # If order price is at or better than BBO, likely taker
        if order.side == "BUY":
            return order.price < book.ask_price
        else:
            return order.price > book.bid_price
```

### Queue Position Model

```python
class QueuePositionModel:
    """Model queue position for limit orders."""

    def __init__(self, queue_factor: float = 0.5):
        self.queue_factor = queue_factor
        self.order_arrival_times: dict[str, int] = {}

    def estimate_fill_probability(self, order: Order,
                                   book: BacktestBook,
                                   ts: int) -> float:
        """
        Estimate probability of fill based on queue position.

        Earlier orders have priority.
        """
        if order.order_id not in self.order_arrival_times:
            self.order_arrival_times[order.order_id] = ts

        time_in_queue = ts - self.order_arrival_times[order.order_id]

        # Get total quantity at price level
        if order.side == "BUY":
            level_qty = self._get_qty_at_price(book.bids, order.price)
        else:
            level_qty = self._get_qty_at_price(book.asks, order.price)

        if level_qty == 0:
            return 1.0  # No competition

        # Simple model: probability increases with time in queue
        base_prob = float(order.quantity / (level_qty + order.quantity))
        time_bonus = min(0.5, time_in_queue / 60000 * 0.1)  # Max 50% bonus

        return min(1.0, base_prob + time_bonus)
```

---

## 11.5 Backtest Engine

```python
class BacktestEngine:
    """Main backtest execution engine."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.fee_model = FeeModel(
            maker_fee_bps=config.maker_fee_bps,
            taker_fee_bps=config.taker_fee_bps,
        )
        self.fill_simulator = FillSimulator(config.fill_config)
        self.state = BacktestState()

    def run(self, data: BacktestData,
            policy: GridPolicy) -> BacktestResult:
        """Run backtest on historical data."""

        # Initialize
        self.state = BacktestState(
            initial_capital=self.config.initial_capital,
        )

        # Event loop
        for event in data.events():
            if event.type == "TRADE":
                self._process_trade(event)
            elif event.type == "BOOK":
                self._process_book(event, policy)
            elif event.type == "FUNDING":
                self._process_funding(event)

        # Generate results
        return self._generate_results()

    def _process_trade(self, trade: BacktestTrade) -> None:
        """Process trade event - check for fills."""
        for order in self.state.pending_orders:
            fill = self.fill_simulator.check_fill(
                order, trade, self.state.current_book
            )
            if fill:
                self._execute_fill(fill)

    def _process_book(self, book: BacktestBook,
                      policy: GridPolicy) -> None:
        """Process book update - run policy."""
        self.state.current_book = book

        # Update features
        features = self._compute_features(book)

        # Get policy decision
        plan, risk = policy.evaluate(
            features,
            self.state.position,
            self.config.policy_config
        )

        # Update grid orders
        self._update_grid(plan)

    def _execute_fill(self, fill: Fill) -> None:
        """Execute fill and update state."""
        # Calculate fee
        notional = fill.price * fill.quantity
        fee = calculate_fee(notional, fill.is_maker, self.fee_model)

        # Update position
        if fill.side == "BUY":
            self.state.position += fill.quantity
        else:
            self.state.position -= fill.quantity

        # Update P&L
        self.state.realized_pnl -= fee
        self.state.total_fees += fee

        # Record fill
        self.state.fills.append(fill)

        # Check for round-trip
        self._check_round_trip(fill)
```

---

## 11.6 Walk-Forward Validation

```python
class WalkForwardValidator:
    """Walk-forward validation framework."""

    def __init__(self, config: WalkForwardConfig):
        self.config = config

    def validate(self, data: BacktestData,
                 param_space: dict) -> WalkForwardResult:
        """
        Run walk-forward validation.

        1. Split data into windows
        2. For each window: optimize on train, test on validation
        3. Aggregate out-of-sample performance
        """
        windows = self._create_windows(data)
        results = []

        for i, window in enumerate(windows):
            logger.info(f"Walk-forward window {i+1}/{len(windows)}")

            # Optimize on training data
            best_params = self._optimize(
                window.train_data,
                param_space
            )

            # Test on validation data
            oos_result = self._test(
                window.validation_data,
                best_params
            )

            results.append(WalkForwardWindowResult(
                window_id=i,
                train_start=window.train_start,
                train_end=window.train_end,
                test_start=window.test_start,
                test_end=window.test_end,
                best_params=best_params,
                in_sample_metrics=self._get_is_metrics(window, best_params),
                out_of_sample_metrics=oos_result,
            ))

        return self._aggregate_results(results)

    def _create_windows(self, data: BacktestData) -> list[ValidationWindow]:
        """Create rolling windows."""
        windows = []

        train_size = self.config.train_days * 86400_000
        test_size = self.config.test_days * 86400_000
        step_size = self.config.step_days * 86400_000

        start = data.start_ts
        end = data.end_ts

        current = start
        while current + train_size + test_size <= end:
            windows.append(ValidationWindow(
                train_start=current,
                train_end=current + train_size,
                test_start=current + train_size,
                test_end=current + train_size + test_size,
                train_data=data.slice(current, current + train_size),
                validation_data=data.slice(
                    current + train_size,
                    current + train_size + test_size
                ),
            ))
            current += step_size

        return windows
```

### Walk-Forward Configuration

```yaml
# config/walkforward.yaml
walk_forward:
  train_days: 30
  test_days: 7
  step_days: 7
  min_windows: 8

  optimization:
    method: "optuna"  # or "grid", "random"
    n_trials: 100
    metric: "sharpe_ratio"

  param_space:
    spacing_bps:
      type: "float"
      low: 5.0
      high: 30.0
    levels:
      type: "int"
      low: 3
      high: 10
    tox_threshold:
      type: "float"
      low: 0.5
      high: 3.0
```

---

## 11.7 Performance Metrics

```python
@dataclass
class BacktestMetrics:
    """Backtest performance metrics."""

    # Returns
    total_return_pct: float
    annualized_return_pct: float

    # Risk
    max_drawdown_pct: float
    volatility_ann: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    # Trading
    total_trades: int
    round_trips: int
    win_rate: float
    profit_factor: float
    avg_win_bps: float
    avg_loss_bps: float

    # Costs
    total_fees_usd: Decimal
    total_slippage_bps: float
    avg_cost_per_rt_bps: float

    # Time
    time_in_market_pct: float
    avg_hold_time_s: float

def calculate_metrics(result: BacktestResult) -> BacktestMetrics:
    """Calculate all performance metrics."""
    equity_curve = result.equity_curve
    returns = np.diff(equity_curve) / equity_curve[:-1]

    # Sharpe
    excess_returns = returns - 0  # Assuming 0 risk-free rate
    sharpe = np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(365 * 24)

    # Sortino (downside deviation)
    downside_returns = returns[returns < 0]
    downside_std = np.std(downside_returns) if len(downside_returns) > 0 else 0.001
    sortino = np.mean(excess_returns) / downside_std * np.sqrt(365 * 24)

    # Max drawdown
    peak = np.maximum.accumulate(equity_curve)
    drawdown = (peak - equity_curve) / peak
    max_dd = np.max(drawdown)

    # Round-trip metrics
    winning_rts = [rt for rt in result.round_trips if rt.net_pnl > 0]
    losing_rts = [rt for rt in result.round_trips if rt.net_pnl <= 0]

    win_rate = len(winning_rts) / len(result.round_trips) if result.round_trips else 0

    return BacktestMetrics(
        total_return_pct=float((equity_curve[-1] - equity_curve[0]) / equity_curve[0] * 100),
        annualized_return_pct=...,
        max_drawdown_pct=float(max_dd * 100),
        volatility_ann=float(np.std(returns) * np.sqrt(365 * 24) * 100),
        sharpe_ratio=float(sharpe),
        sortino_ratio=float(sortino),
        calmar_ratio=float(np.mean(returns) * 365 * 24 / max_dd) if max_dd > 0 else 0,
        total_trades=len(result.fills),
        round_trips=len(result.round_trips),
        win_rate=win_rate,
        # ... more metrics
    )
```

---

## 11.8 Reporting

```python
def generate_backtest_report(result: BacktestResult,
                             output_dir: Path) -> None:
    """Generate comprehensive backtest report."""

    metrics = calculate_metrics(result)

    # 1. Summary table
    summary = {
        "Period": f"{result.start_date} to {result.end_date}",
        "Total Return": f"{metrics.total_return_pct:.2f}%",
        "Sharpe Ratio": f"{metrics.sharpe_ratio:.2f}",
        "Max Drawdown": f"{metrics.max_drawdown_pct:.2f}%",
        "Win Rate": f"{metrics.win_rate:.1%}",
        "Round Trips": metrics.round_trips,
        "Total Fees": f"${metrics.total_fees_usd:.2f}",
    }

    # 2. Equity curve chart
    plot_equity_curve(result.equity_curve, output_dir / "equity.png")

    # 3. Drawdown chart
    plot_drawdown(result.equity_curve, output_dir / "drawdown.png")

    # 4. Monthly returns heatmap
    plot_monthly_returns(result, output_dir / "monthly.png")

    # 5. Trade distribution
    plot_trade_distribution(result.round_trips, output_dir / "trades.png")

    # 6. Policy breakdown
    plot_policy_performance(result, output_dir / "policies.png")

    # 7. Save metrics JSON
    with open(output_dir / "metrics.json", "w") as f:
        json.dump(asdict(metrics), f, indent=2, default=str)
```

---

## 11.9 Determinism & Reproducibility

```python
class DeterministicBacktest:
    """Ensure backtest reproducibility."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        np.random.seed(seed)
        random.seed(seed)

    def run_with_hash(self, data: BacktestData,
                      config: BacktestConfig) -> tuple[BacktestResult, str]:
        """Run backtest and return result with deterministic hash."""

        # Hash inputs
        data_hash = self._hash_data(data)
        config_hash = self._hash_config(config)

        # Run backtest
        result = self._run(data, config)

        # Hash outputs
        result_hash = self._hash_result(result)

        # Combined hash for reproducibility verification
        combined_hash = hashlib.sha256(
            f"{data_hash}:{config_hash}:{result_hash}".encode()
        ).hexdigest()[:16]

        return result, combined_hash

    def verify_reproducibility(self, result1: BacktestResult,
                                result2: BacktestResult) -> bool:
        """Verify two backtest runs produced identical results."""
        return (
            self._hash_result(result1) == self._hash_result(result2)
        )
```

---

## 11.10 Baselines for Adaptive Controller changes

Any PR that changes **regime selection**, **adaptive step**, **auto-reset**, or **policy parameters** must include a baseline comparison.

**Baseline A — Static Grid (control)**
- fixed `spacing_bps`
- fixed levels
- no regime selection
- `reset_action = NONE`

**Baseline B — Adaptive Controller (treatment)**
- regime selection enabled
- adaptive `spacing_bps`
- reset behavior enabled (SOFT/HARD)

### Required metrics

In addition to PnL/Sharpe/Drawdown, report execution-quality metrics:
- Fill rate (per side)
- Cancel/replace rate (order churn)
- Adverse selection proxy (e.g., mid-move after fill)
- Slippage vs mid (bps)

### Reporting format

- `metrics.json` MUST include both baselines keyed by `baseline_id`.
- If Baseline B improves PnL but degrades execution quality (churn/adverse selection), the PR must explain why it is acceptable.
