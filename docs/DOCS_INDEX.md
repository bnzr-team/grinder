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
- `docs/13_OBSERVABILITY.md`
- `docs/14_GITHUB_WORKFLOW.md`

---

## Spec vs reality
- Specs define **target behavior**.
- `STATE.md` defines **current behavior**.
- If a spec conflicts with `STATE.md`, treat the spec as *planned* unless `STATE.md` says implemented.
