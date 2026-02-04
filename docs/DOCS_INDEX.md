# DOCS INDEX — Grinder

Where to find information:

- **Implementation truth:** `docs/STATE.md`
- **Decisions:** `docs/DECISIONS.md`
- **Plan + progress:** `docs/ROADMAP.md`

---

## Core orientation
- `docs/00_PRODUCT.md` — goals, scope, milestone checklists (target spec)
- `docs/STATE.md` — what is implemented *right now*
- `docs/DECISIONS.md` — why key choices were made
- `docs/ROADMAP.md` — progress tracker + traceability + M1 DoD

---

## Glossary & data
- `docs/01_GLOSSARY.md`
- `docs/02_DATA_SOURCES.md`
- `docs/05_FEATURE_CATALOG.md`
- `docs/15_CONSTANTS.md`

---

## Architecture & system behavior
- `docs/03_ARCHITECTURE.md`
- `docs/08_STATE_MACHINE.md`

---

## Pipeline specs (target behavior)
- `docs/04_PREFILTER_SPEC.md`
- `docs/06_TOXICITY_SPEC.md`
- `docs/07_GRID_POLICY_LIBRARY.md`
- `docs/09_EXECUTION_SPEC.md`
- `docs/10_RISK_SPEC.md`
- `docs/11_BACKTEST_PROTOCOL.md`
- `docs/12_ML_SPEC.md`
- `docs/16_ADAPTIVE_GRID_CONTROLLER_SPEC.md` — meta-controller (regime, step, reset)
- `docs/13_OBSERVABILITY.md`
- `docs/14_GITHUB_WORKFLOW.md`

---

## Smart Grid (versioned specs)
- `docs/smart_grid/README.md` — overview + version matrix
- `docs/smart_grid/ROADMAP.md` — feature roadmap by version
- `docs/smart_grid/SPEC_V1_0.md` — v1.0 base (L1-only, deterministic regime)
- `docs/smart_grid/SPEC_V1_1.md` — v1.1 (+FeatureEngine, NATR/ATR)
- `docs/smart_grid/SPEC_V1_2.md` — v1.2 (+AdaptiveGridPolicy, dynamic sizing)
- `docs/smart_grid/SPEC_V1_3.md` — v1.3 (+Top-K v1, feature-based selection)
- `docs/smart_grid/SPEC_V2_0.md` — v2.0 (L2-aware, partial fills, DD allocator)
- `docs/smart_grid/SPEC_V3_0.md` — v3.0 (multi-venue, full production)

---

## Operations
- `docs/HOW_TO_OPERATE.md` — operator's guide (includes Release Checklist v1)
- `docs/OBSERVABILITY_STACK.md` — Prometheus + Grafana setup
- `docs/runbooks/` — operational runbooks:
  - `01_STARTUP_SHUTDOWN.md` — starting/stopping the system
  - `02_HEALTH_TRIAGE.md` — quick health checks
  - `03_METRICS_DASHBOARDS.md` — Prometheus metrics and Grafana
  - `04_KILL_SWITCH.md` — kill-switch events and recovery
  - `05_SOAK_GATE.md` — running soak tests
  - `06_ALERT_RESPONSE.md` — responding to alerts
  - `07_HA_OPERATIONS.md` — HA deployment, failover, rolling restart

---

## Spec vs reality
- Specs define **target behavior**.
- `STATE.md` defines **current behavior**.
- If a spec conflicts with `STATE.md`, treat the spec as *planned* unless `STATE.md` says implemented.
