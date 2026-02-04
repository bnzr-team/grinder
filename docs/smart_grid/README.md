# Smart Grid — Versioned Specifications

This folder contains **versioned, full-text specifications** for the Smart Grid system.

## Single Source of Truth (SSOT)

- `docs/STATE.md` — **reality**: what is implemented *right now*.
- `docs/DECISIONS.md` — **ADRs**: why contracts/behavior changed.
- `docs/smart_grid/SPEC_V*.md` — **specs**: how a given version is intended to work.
- `docs/smart_grid/ROADMAP.md` — **version roadmap** and DoD gates.

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
