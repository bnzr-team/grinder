# 🔥 GRINDER — Adaptive Grid Trading System

## SSOT Documentation Pack v1.0.0 (Enterprise Edition)

> **Codename**: Grinder
> **Tagline**: "Grind the markets, not your nerves"
> **Version**: 1.0.0-enterprise
> **Status**: Production-ready specification

> **Implementation status is tracked in:** `docs/STATE.md`
> **Plan + progress is tracked in:** `docs/ROADMAP.md`

---

## 0.1 Mission Statement

**GRINDER** is an enterprise-grade adaptive grid trading system for crypto perpetuals
that maximizes risk-adjusted returns through intelligent market microstructure analysis,
dynamic policy selection, and robust risk management.

---

## 0.2 Core Value Proposition

```
┌─────────────────────────────────────────────────────────────────┐
│                         GRINDER                                  │
├─────────────────────────────────────────────────────────────────┤
│  "We don't predict markets. We adapt to them."                  │
│                                                                  │
│  ✓ Cost-aware grid policies (profitable after fees)             │
│  ✓ Real-time toxicity detection (avoid adverse selection)       │
│  ✓ Multi-regime operation (range, trend, panic, recovery)       │
│  ✓ ML-calibrated parameters (not black-box trading)             │
│  ✓ Enterprise reliability (HA, audit, compliance)               │
└─────────────────────────────────────────────────────────────────┘
```

---

## 0.3 Target Users

| User | Use Case |
|------|----------|
| **Prop Desk** | Automated grid strategies with risk controls |
| **Quant Fund** | Alpha generation + market making hybrid |
| **Solo Trader** | Hands-off grid trading with safety rails |
| **Market Maker** | Inventory management + spread capture |

---

## 0.4 Objectives Hierarchy

```
L1: SURVIVAL
├── Never lose more than DD_MAX_DAILY
├── Always maintain system health
└── Graceful degradation over crash

L2: CONSISTENCY
├── Positive RT expectancy after costs
├── Stable fill rates across regimes
└── Predictable behavior (reason codes)

L3: GROWTH
├── Maximize Sharpe ratio
├── Scale to more symbols/capital
└── Continuous improvement via ML
```

---

## 0.5 Success Metrics (KPIs)

| Metric | Target | Critical | Description |
|--------|--------|----------|-------------|
| `sharpe_ratio` | > 2.0 | > 1.0 | Risk-adjusted returns |
| `rt_expectancy_bps` | > 3.0 | > 1.5 | Avg bps per round-trip |
| `rt_fill_rate` | > 40% | > 25% | % of levels completing RT |
| `max_dd_pct` | < 5% | < 10% | Max drawdown |
| `uptime_pct` | > 99.5% | > 99% | System availability |
| `latency_p99_ms` | < 100 | < 500 | Decision latency |

---

## 0.6 Non-Goals (Explicit)

- ❌ Pure directional speculation
- ❌ News/event-driven trading (v2 maybe)
- ❌ Cross-exchange arbitrage (separate system)
- ❌ Options trading
- ❌ Spot market (perps only for now)

---

## 0.7 Key Design Principles

### 0.7.1 Cost-First
No policy may assume fills inside spread. Always use effective spread/impact calculations.

### 0.7.2 Top-K L2
Keep infrastructure scalable. L2 depth data only for Top-K selected symbols.

### 0.7.3 Safety by Construction
Kill-switches and inventory caps are mandatory, not optional.

### 0.7.4 Repeatability
Every decision must be explainable by logged reason codes.

### 0.7.5 Graceful Degradation
System degrades functionality rather than crashes on partial failures.

---

## 0.8 System Boundaries

### In Scope (v1.0)
- Binance Futures USDT-M perpetuals
- Grid trading (bilateral + unidirectional)
- Real-time L1/L2 data processing
- ML-calibrated parameters
- Risk management & monitoring

### Out of Scope (v1.0)
- Multi-exchange routing
- Spot market
- Options
- Social/news signals
- Portfolio optimization across strategies

---

## 0.9 Success Criteria for Launch

### MVP (v0.1)
- [ ] Single symbol grid working
- [ ] Basic risk limits enforced
- [ ] Backtest framework operational
- [ ] Manual mode switching

### Beta (v0.5)
- [ ] Top-K prefilter working
- [ ] All 6 grid policies implemented
- [ ] Toxicity detection active
- [ ] Paper trading validated

### Production (v1.0)
- [ ] HA deployment ready
- [ ] ML calibration pipeline
- [ ] Full observability
- [ ] Documented runbooks
