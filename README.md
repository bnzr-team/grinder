# GRINDER

> "Grind the markets, not your nerves"

Enterprise-grade adaptive grid trading system for crypto perpetuals.

## Overview

GRINDER is an intelligent grid trading bot that maximizes risk-adjusted returns through:

- **Market Microstructure Analysis** - L1/L2 order book features, OFI, toxicity detection
- **Dynamic Policy Selection** - 6 grid policies adapting to market regimes
- **Robust Risk Management** - Inventory caps, drawdown limits, emergency procedures
- **ML Calibration** - Offline parameter optimization with walk-forward validation

## Key Features

| Feature | Description |
|---------|-------------|
| **Top-K L2** | L2 depth data only for selected high-opportunity symbols |
| **Toxicity Gating** | Pause/throttle when adverse selection risk is high |
| **Multi-Regime** | Range, trend, funding harvest, liquidation recovery |
| **Cost-Aware** | All policies profitable after fees and slippage |
| **Enterprise-Ready** | HA, audit trails, observability, compliance |

## Quick Start

### Installation

```bash
# Clone repository
git clone https://github.com/grinder/grinder.git
cd grinder

# Install with all dependencies
pip install -e ".[all]"

# Or minimal installation
pip install -e .
```

### Configuration

```bash
# Copy example config
cp config/example.yaml config/local.yaml

# Set API credentials (never commit these)
export BINANCE_API_KEY="your-api-key"
export BINANCE_API_SECRET="your-api-secret"
```

### Paper Trading

```bash
# Start paper trading
grinder-paper --config config/local.yaml

# With specific symbols
grinder-paper --config config/local.yaml --symbols BTCUSDT,ETHUSDT
```

### Backtesting

```bash
# Run backtest
grinder-backtest --config config/backtest.yaml --data data/2024-01/

# Generate report
grinder-backtest --config config/backtest.yaml --data data/2024-01/ --report
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         GRINDER                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐     │
│  │ Binance  │   │  Feature │   │  Policy  │   │Execution │     │
│  │Connector │──▶│  Engine  │──▶│  Engine  │──▶│  Engine  │     │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘     │
│       │              │              │              │            │
│       │              │              │              │            │
│       ▼              ▼              ▼              ▼            │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐     │
│  │   Data   │   │ Toxicity │   │   Risk   │   │  Order   │     │
│  │ Quality  │   │ Detector │   │ Manager  │   │ Manager  │     │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Observability                          │   │
│  │              (Metrics / Logs / Traces)                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Grid Policies

| Policy | Mode | Use Case |
|--------|------|----------|
| **Range Grid** | BILATERAL | Low volatility, mean-reverting markets |
| **Trend Follower** | UNI_LONG/SHORT | Sustained directional moves |
| **Funding Harvester** | BILATERAL (biased) | Extreme funding rates |
| **Liquidation Catcher** | UNI | Post-cascade mean reversion |
| **Volatility Breakout** | UNI | Compression → expansion |
| **Mean Reversion Sniper** | UNI | Fade extreme moves with exhaustion |

## Documentation

| Document | Description |
|----------|-------------|
| [00_PRODUCT.md](docs/00_PRODUCT.md) | Product specification |
| [01_GLOSSARY.md](docs/01_GLOSSARY.md) | Terminology definitions |
| [05_FEATURE_CATALOG.md](docs/05_FEATURE_CATALOG.md) | Feature specifications |
| [07_GRID_POLICY_LIBRARY.md](docs/07_GRID_POLICY_LIBRARY.md) | Policy implementations |
| [XX_CONSTANTS.md](docs/XX_CONSTANTS.md) | Default parameters |

## Project Structure

```
grinder/
├── src/grinder/
│   ├── connectors/      # Exchange connectivity
│   │   └── binance/     # Binance Futures integration
│   ├── data/            # Data quality & storage
│   ├── features/        # Feature calculations
│   ├── policies/        # Grid policies
│   │   └── grid/        # Grid policy implementations
│   ├── execution/       # Order execution
│   ├── risk/            # Risk management
│   ├── backtest/        # Backtesting framework
│   ├── ml/              # ML calibration
│   └── ops/             # Operations & observability
├── tests/
│   ├── unit/            # Unit tests
│   ├── integration/     # Integration tests
│   └── fixtures/        # Test data
├── docs/                # Documentation
├── config/              # Configuration files
├── monitoring/          # Grafana dashboards
├── k8s/                 # Kubernetes manifests
└── scripts/             # Utility scripts
```

## Development

### Setup

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
```

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=grinder --cov-report=html

# Run specific test file
pytest tests/unit/test_policies.py

# Run slow tests
pytest -m slow
```

### Code Quality

```bash
# Lint
ruff check .

# Format
ruff format .

# Type check
mypy src/grinder
```

## Success Metrics

| Metric | Target | Critical |
|--------|--------|----------|
| Sharpe Ratio | > 2.0 | > 1.0 |
| RT Expectancy | > 3 bps | > 1.5 bps |
| RT Fill Rate | > 40% | > 25% |
| Max Drawdown | < 5% | < 10% |
| Uptime | > 99.5% | > 99% |
| Latency P99 | < 100ms | < 500ms |

## Risk Warnings

- This software is for educational and research purposes
- Trading cryptocurrencies involves substantial risk of loss
- Past performance does not guarantee future results
- Never trade with funds you cannot afford to lose
- Always start with paper trading before using real funds

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

Please ensure:
- All tests pass
- Code follows project style (ruff)
- Type hints are complete (mypy)
- Documentation is updated
