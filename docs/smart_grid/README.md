# Smart Grid — Versioned Specifications

This folder contains **versioned, full-text specifications** for the Smart Grid system.

## Version Status

| Version | Spec | Status | Proof (fixtures/ADRs) |
|---------|------|--------|----------------------|
| v1.0 | `SPEC_V1_0.md` | ✅ Implemented | `sample_day`, `sample_day_allowed`; ADR-019..021 |
| v1.1 | `SPEC_V1_1.md` | ✅ Implemented | FeatureEngine in `sample_day_adaptive`; ADR-019 |
| v1.2 | `SPEC_V1_2.md` | ✅ Implemented | `sample_day_adaptive` digest `1b8af993a8435ee6`; ADR-022 |
| v1.3 | `SPEC_V1_3.md` | ✅ Implemented | `sample_day_topk_v1` digest `63d981b60a8e9b3a`; ADR-023 |
| v2.0 | `SPEC_V2_0.md` | 🔜 Planned | L2-aware, DD allocator — see ROADMAP M7 |
| v3.0 | `SPEC_V3_0.md` | 🔜 Planned | Multi-venue — see ROADMAP M9 |

**Current target:** v1.3 (implemented)
**Next planned:** v2.0 (M7 milestone)

> **Canonical status:** See `docs/STATE.md` §Smart Grid Spec Version

---

## Single Source of Truth (SSOT)

- `docs/STATE.md` — **reality**: what is implemented *right now*.
- `docs/DECISIONS.md` — **ADRs**: why contracts/behavior changed.
- `docs/smart_grid/SPEC_V*.md` — **specs**: how a given version is intended to work.
- `docs/smart_grid/ROADMAP.md` — **version roadmap** and DoD gates.
- `docs/ROADMAP.md` — **main roadmap** with M7–M9 milestones for v2.0+.

**Rule:** A spec version may be marked **Implemented** in `docs/STATE.md` only if:
1) Unit tests exist
2) Fixtures cover the new behaviors
3) Determinism suite digests are updated and stable
4) ADRs exist for any contract/behavior change
5) `/healthz` and `/metrics` contracts are updated if touched

## Naming & Layout

- `SPEC_V1_0.md` … `SPEC_V3_0.md` — complete, standalone documents (not diffs).
- `ROADMAP.md` — criteria for bumping versions and acceptance gates.

## Bumping a Version

1) Copy the previous spec file (e.g., `SPEC_V1_0.md` → `SPEC_V1_1.md`).
2) Integrate changes into the **full text** (do not publish “diff-only” specs).
3) Add ADR(s) in `docs/DECISIONS.md` for any contract/behavior changes.
4) Add/extend fixtures and tests; update determinism digests.
5) Update `docs/STATE.md`:
   - `Implemented: vX.Y` (only after proofs)
   - `Planned next: vA.B`

## Linking Tests & Fixtures to Specs

Recommended:
- In tests, annotate which spec section is validated:
  - `# Validates: docs/smart_grid/SPEC_V1_2.md §17.14`
- In fixture metadata (or filenames), include the spec version:
  - `...__v1_2.json` or `"meta": {"spec_version": "v1.2"}`

## What this folder does NOT do

- It does not claim implementation by itself.
- It does not replace `STATE.md`.
