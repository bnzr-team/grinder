# Documentation vs Implementation Gaps

> SSOT index of where specs describe target-state behavior beyond current code.
>
> Last updated: 2026-02-17

This file tracks the delta between documented specifications and actual implementation.
Each spec may describe both current reality and planned features — this index clarifies which is which.

---

## Gap Index

| Component | Spec | Code | Status | Notes / Tracking |
|-----------|------|------|--------|------------------|
| Toxicity formulas (VPIN, Kyle, Amihud, OFI, liquidation surge) | `docs/06_TOXICITY_SPEC.md` | `src/grinder/gating/toxicity_gate.py` | **PARTIAL** | v0: spread_spike + price_impact only; composite scoring planned post-launch |
| Grid policy library (Trend, LiqCatcher, Funding, VolBreakout, MeanRev) | `docs/07_GRID_POLICY_LIBRARY.md` | `src/grinder/policies/grid/` | **PARTIAL** | 2 of ~6 policies: Static + Adaptive; others are target state |
| Backtest engine (walk-forward, cost model, queue modeling) | `docs/11_BACKTEST_PROTOCOL.md` | `src/grinder/backtest/cli.py` | **DEFERRED** | 29-line stub delegates to replay; full engine deferred to M9 |
| State machine orchestrator (formal FSM, state persistence) | `docs/08_STATE_MACHINE.md` | `src/grinder/controller/regime.py` | **PARTIAL** | Regime classifier implemented; no centralized FSM orchestrator |
| Smart order routing (amend vs cancel-replace, batching) | `docs/09_EXECUTION_SPEC.md` §9.3 | `src/grinder/execution/engine.py` | **PARTIAL** | Core engine done (670 lines); SmartOrderRouter / batch ops planned |
| Fill tracking (FillTracker, RoundTrip, PositionSyncer) | `docs/09_EXECUTION_SPEC.md` §9.6–9.7 | `src/grinder/execution/` | **PARTIAL** | Partial in paper engine; standalone modules not built |
| Latency / retry (LatencyMonitor, OrderRetryPolicy) | `docs/09_EXECUTION_SPEC.md` §9.8–9.9 | — | **PLANNED** | Not implemented |
| Fill probability model (`estimate_fill_probability()`) | `docs/09_EXECUTION_SPEC.md` §9.4 | — | **PLANNED** | Not implemented |
| Portfolio risk (beta-adjusted exposure, concentration) | `docs/10_RISK_SPEC.md` §10.5 | `src/grinder/risk/` | **PARTIAL** | DrawdownGuard v1 + KillSwitch done; PortfolioRiskManager not built |
| Consecutive loss limit | `docs/10_RISK_SPEC.md` | `src/grinder/risk/` | **PLANNED** | Not implemented; daily loss limit exists |
| ML training pipeline (sklearn→ONNX, walk-forward datasets) | `docs/12_ML_SPEC.md` §12.9 | `scripts/train_regime_model.py` | **PARTIAL** | Script exists but training logic is stub; ONNX infra fully done (M8) |
| ML policy integration (signal → grid param adjustment) | `docs/12_ML_SPEC.md` §12.5 | `src/grinder/ml/` | **PLANNED** | MlSignalSnapshot computed but not consumed by AdaptiveGridPolicy |
| ML drift detection / monitoring | `docs/12_ML_SPEC.md` §12.11 | — | **PLANNED** | Not implemented |
| Feature store (offline repo, versioning, lineage) | `docs/12_ML_SPEC.md` §12.8 | — | **PARTIAL** | Dataset artifact pipeline done (M8-04); FeatureStore module/service planned |
| Advanced features (OFI, CVD, VAMP, multi-timeframe) | `docs/05_FEATURE_CATALOG.md` | `src/grinder/features/` | **PARTIAL** | ~10 core features done; momentum, OFI, CVD, multi-TF not built |
| Multi-venue (Bybit, OKX, COIN-M) | `docs/02_DATA_SOURCES.md` | `src/grinder/execution/` | **DEFERRED** | Binance USDT-M only; deferred to M9 post-launch (ADR-066) |
| Data quality (GapDetector, outlier filtering) | `docs/02_DATA_SOURCES.md` | `src/grinder/data/` | **PARTIAL** | Basic staleness checks; no GapDetector or outlier filtering |

---

## Status Legend

| Status | Meaning |
|--------|---------|
| **DONE** | Spec matches code — no gap |
| **PARTIAL** | Core functionality implemented; spec describes additional planned features |
| **PLANNED** | Spec exists; no implementation yet |
| **DEFERRED** | Explicitly deferred to a future milestone (with ADR or roadmap reference) |

---

## Fully Implemented (no gap)

These specs match their implementation — not listed in the gap table above:

- **Adaptive Grid Policy v1/v2** — `docs/17_ADAPTIVE_SMART_GRID_V1.md` ↔ `src/grinder/policies/grid/adaptive.py` (734 lines)
- **Prefilter / Top-K** — `docs/04_PREFILTER_SPEC.md` ↔ `src/grinder/prefilter/`
- **ML ONNX contracts (M8-01/02)** — `docs/12_ML_SPEC.md` §12.2 ↔ `src/grinder/ml/onnx/` (1,817 lines)
- **ML model registry & promotion** — `docs/12_ML_SPEC.md` §12.10 ↔ `ml/registry/` + `scripts/promote_ml_model.py`
- **Constraint Provider (M7-05)** — `docs/09_EXECUTION_SPEC.md` ↔ `src/grinder/execution/constraint_provider.py`
- **Drawdown Guard v1** — `docs/10_RISK_SPEC.md` §10.4 ↔ `src/grinder/risk/drawdown_guard_v1.py`
- **Auto-Sizer / DD Allocator** — `docs/10_RISK_SPEC.md` ↔ `src/grinder/sizing/`
- **GitHub Workflow** — `docs/14_GITHUB_WORKFLOW.md` ↔ `.github/workflows/`
