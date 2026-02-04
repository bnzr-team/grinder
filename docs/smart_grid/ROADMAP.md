# ROADMAP — Adaptive Smart Grid Specs (Versioned)

This roadmap explains how to structure versioned “full text” specifications and when to publish the next version.

## Files
- `docs/smart_grid/SPEC_V1_0.md` — v1.0 baseline (L1-only, deterministic regime)
- `docs/smart_grid/SPEC_V1_1.md` — v1.1 (+FeatureEngine, NATR/ATR)
- `docs/smart_grid/SPEC_V1_2.md` — v1.2 (+AdaptiveGridPolicy, dynamic sizing)
- `docs/smart_grid/SPEC_V1_3.md` — v1.3 (+Top-K v1, feature-based selection)
- `docs/smart_grid/SPEC_V2_0.md` — v2.0 (L2-aware, partial fills, DD allocator)
- `docs/smart_grid/SPEC_V3_0.md` — v3.0 (multi-venue, full production)
- `docs/smart_grid/ROADMAP.md` — this file

**Rule:** every spec file is **complete** (not a diff).  
When bumping a version, start by copying the previous file and then integrate changes, plus refresh the addendum.

## Version gates (Definition of Done)
A version may be marked “Implemented” in `docs/STATE.md` only if:
1) Unit tests exist
2) Fixtures cover the new behaviors
3) Determinism suite digests are updated and stable
4) `docs/DECISIONS.md` includes ADR(s) for any contract/behavior change
5) `/healthz` and `/metrics` contracts are updated if touched

## When to bump versions
### v1.0 → v1.1 (Execution realism)
- fees/funding included
- bounded deterministic partial fills
- deterministic latency knobs
- new fill metrics (fill-rate, time-to-fill)

### v1.1 → v1.2 (L2-first-class)
- Snapshot.book used broadly
- impact/spread-at-depth gating & sizing
- deterministic walk-the-book execution

### v1.2 → v1.3 (Portfolio survivability)
- dynamic allocator across Top-K
- concentration/correlation guard
- deterministic damage-control playbooks

### v1.3 → v2.0 (ML-assisted)
- pinned artifacts by hash
- offline calibration pipeline
- inference determinism tests

### v2.0 → v3.0 (RL allowed)
- environment realism prevents simulator cheating
- offline RL governance + evaluation protocol

## Repo structure
- Specs: `docs/smart_grid/SPEC_V*.md` (versioned)
- ADRs: `docs/DECISIONS.md`
- Reality: `docs/STATE.md`
- Tests/fixtures must reference the version they validate in comments or metadata.
